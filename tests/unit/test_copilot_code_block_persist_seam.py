"""Tests for the code-block persist seam in `_update_workflow`.

OSS-synced: only example.* / RFC-2606 placeholder targets and synthetic labels.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import re
import textwrap
from collections.abc import Iterable
from pathlib import Path
from types import EllipsisType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
import yaml
from structlog.testing import capture_logs

from skyvern.forge.sdk.copilot import agent as agent_module
from skyvern.forge.sdk.copilot import code_block_synthesis as code_block_synthesis_module
from skyvern.forge.sdk.copilot import enforcement as enforcement_module
from skyvern.forge.sdk.copilot import request_policy as request_policy_module
from skyvern.forge.sdk.copilot import tools as tools_module
from skyvern.forge.sdk.copilot.authoring_parameter_binding import (
    AuthoringParameterBindingCandidate,
    build_authoring_parameter_binding_directive,
)
from skyvern.forge.sdk.copilot.blocker_signal import (
    CREDENTIAL_SCOUT_VERIFY_REPLY,
    CopilotToolBlockerSignal,
    assert_clean_user_facing_text,
)
from skyvern.forge.sdk.copilot.build_test_outcome import (
    BuildTestOutcomeReasonCode,
    RecordedBuildTestOutcome,
    RecordedOutcomeBindingConstraint,
    authored_structure_signature_from_workflow,
    record_build_test_outcome,
    recorded_outcome_from_author_time_reject,
    recorded_outcome_from_run_blocks_result,
)
from skyvern.forge.sdk.copilot.code_block_preflight import (
    SANDBOX_UNRESOLVED_NAME_REASON_CODE,
    _sandbox_shim_surface,
    strip_redundant_sandbox_imports,
)
from skyvern.forge.sdk.copilot.code_block_security import CodeBlockSecurityError
from skyvern.forge.sdk.copilot.code_block_synthesis import (
    _MAX_STEPS,
    ProducedStaticReturnEnvelope,
    ScoutGap,
    SynthesisDiagnostics,
    SynthesizedCodeBlock,
    _get_by_role_expr,
    _get_by_role_expr_strict,
    _stable_same_kind_bare_click_refiner,
    credential_scout_gap,
    produce_covered_static_return_envelope,
)
from skyvern.forge.sdk.copilot.completion_verification import CompletionVerificationResult, CriterionVerdict
from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy, CopilotConfig
from skyvern.forge.sdk.copilot.context import CodeAuthoringRepairContext, CopilotContext, FillCarry
from skyvern.forge.sdk.copilot.output_contracts import (
    OutputContractAdvisoryState,
    code_block_available_binding_keys_by_label,
)
from skyvern.forge.sdk.copilot.output_extraction_plan import (
    LiveReadBinding,
    LiveReadKind,
    RequestedOutputExtractionPlan,
    RevealAnchor,
)
from skyvern.forge.sdk.copilot.output_utils import sanitize_tool_result_for_llm
from skyvern.forge.sdk.copilot.reached_download_target import ReachedDownloadTarget
from skyvern.forge.sdk.copilot.request_policy import (
    CompletionCriterion,
    CriterionKind,
    CriterionLevel,
    JudgmentTruthCondition,
    RequestedOutputEvidenceSource,
    RequestPolicy,
)
from skyvern.forge.sdk.copilot.run_outcome import TERMINAL_CHALLENGE_BLOCKER_REASON_CODE, RecordedRunOutcome
from skyvern.forge.sdk.copilot.runtime import AgentContext
from skyvern.forge.sdk.copilot.tools import (
    _code_block_safety_errors,
    _detect_stale_block_metadata,
    _update_workflow,
)
from skyvern.forge.sdk.copilot.tools import run_execution as run_execution_module
from skyvern.forge.sdk.copilot.tools import scouting as scouting_module
from skyvern.forge.sdk.copilot.tools import workflow_update as workflow_update_module
from skyvern.forge.sdk.copilot.tools.workflow_update import (
    _code_safety_reject_payload,
    _OutputContractEvaluation,
    _strip_redundant_sandbox_imports_in_yaml,
)
from skyvern.forge.sdk.copilot.turn_halt import (
    TurnHalt,
    TurnHaltKind,
    TurnHaltVerdict,
)
from skyvern.forge.sdk.copilot.turn_origin import TurnOrigin
from skyvern.forge.sdk.copilot.workflow_credential_utils import parse_workflow_yaml, workflow_blocks
from skyvern.forge.sdk.workflow.exceptions import InsecureCodeDetected
from skyvern.forge.sdk.workflow.models.block import CodeBlock


def _typed_completion_criterion(
    *,
    id: str,
    output_path: str,
    level: CriterionLevel,
    method_mandated: bool,
    kind: CriterionKind,
) -> CompletionCriterion:
    return CompletionCriterion(
        id=id,
        outcome=f"The run returns {output_path}.",
        output_path=output_path,
        level=level,
        method_mandated=method_mandated,
        kind=kind,
    )


def _yaml(body: str) -> str:
    return textwrap.dedent(body).strip() + "\n"


_IMPORTING_CODE_YAML = _yaml(
    """
    title: Registry lookup
    workflow_definition:
      blocks:
      - block_type: code
        label: search_registry
        code: |
          import asyncio
          await page.goto("https://example.com/search")
    """
)

_REQUESTS_IMPORT_CODE_YAML = _yaml(
    """
    title: Registry lookup
    workflow_definition:
      blocks:
      - block_type: code
        label: search_registry
        code: |
          import requests
          await page.goto("https://example.com/search")
    """
)

_ASYNCIO_GATHER_IMPORT_CODE_YAML = _yaml(
    """
    title: Registry lookup
    workflow_definition:
      blocks:
      - block_type: code
        label: search_registry
        code: |
          import asyncio
          await asyncio.gather(page.goto("https://example.com/search"))
    """
)

_SAFE_CODE_YAML = _yaml(
    """
    title: Registry lookup
    workflow_definition:
      blocks:
      - block_type: code
        label: search_registry
        code: |
          await page.goto("https://example.com/search")
    """
)

_SAFE_EXTRACTION_CODE_YAML = _yaml(
    """
    title: Registry lookup
    workflow_definition:
      blocks:
      - block_type: code
        label: search_registry
        code: |
          await page.goto("https://example.com/search")
          records = [{"number": "REC-001"}]
    """
)


def _code_yaml(
    code: str,
    *,
    parameter_keys: list[str] | None = None,
    workflow_param: bool = False,
    nested: bool = False,
) -> str:
    block: dict[str, object] = {
        "block_type": "code",
        "label": "nested_search" if nested else "search_registry",
        "code": textwrap.dedent(code).strip(),
    }
    if parameter_keys is not None:
        block["parameter_keys"] = parameter_keys
    definition: dict[str, object] = {"blocks": [block]}
    if workflow_param:
        definition["parameters"] = [
            {
                "parameter_type": "workflow",
                "workflow_parameter_type": "string",
                "key": "provider_query",
                "default_value": "Sample Search",
            }
        ]
    if nested:
        definition["blocks"] = [
            {
                "block_type": "conditional",
                "label": "choose_path",
                "branch_conditions": [{"condition": "{{ branch_name }} == 'search'", "blocks": [block]}],
            }
        ]
    return yaml.safe_dump({"title": "Registry lookup", "workflow_definition": definition}, sort_keys=False)


_SUBMITTED_LITERAL_YAML = _yaml(
    """
    title: Provider lookup
    workflow_definition:
      blocks:
      - block_type: code
        label: search_registry
        code: |
          await page.locator("input[placeholder='Search']").fill("Sample Search")
          await page.locator("button.lookup").click()
    """
)

_SUBMITTED_COMPUTED_LITERAL_YAML = _yaml(
    """
    title: Provider lookup
    workflow_definition:
      blocks:
      - block_type: code
        label: search_registry
        code: |
          await page.locator("input[placeholder='Search']").fill(provider_name)
    """
)

_SUBMITTED_REPEATED_COMPUTED_LITERAL_YAML = _yaml(
    """
    title: Provider lookup
    workflow_definition:
      blocks:
      - block_type: code
        label: search_registry
        code: |
          await page.locator("input[placeholder='Search']").fill(provider_name)
          await page.locator("#alternate-search").fill(str(provider_name))
    """
)

_SUBMITTED_MIXED_FILL_COMPUTED_LITERAL_YAML = _yaml(
    """
    title: Provider lookup
    workflow_definition:
      blocks:
      - block_type: code
        label: search_registry
        code: |
          form_helper.fill("decorative helper call")
          await page.locator("input[placeholder='Search']").fill(provider_name)
    """
)

_SUBMITTED_MIXED_LOCATOR_FILL_COMPUTED_LITERAL_YAML = _yaml(
    """
    title: Provider lookup
    workflow_definition:
      blocks:
      - block_type: code
        label: search_registry
        code: |
          await page.locator("#org").fill("Sample Org")
          await page.locator("input[placeholder='Search']").fill(provider_name)
    """
)

_SUBMITTED_UNKNOWN_COMPUTED_LITERAL_YAML = _yaml(
    """
    title: Provider lookup
    workflow_definition:
      blocks:
      - block_type: code
        label: search_registry
        code: |
          await page.locator("input[placeholder='Search']").fill(unscouted_provider_name)
    """
)

_SUBMITTED_LOCAL_CONSTANT_YAML = _yaml(
    """
    title: Provider lookup
    workflow_definition:
      blocks:
      - block_type: code
        label: search_registry
        code: |
          provider_query = "Sample Search"
          await page.locator("input[placeholder='Search']").fill(str(provider_query))
    """
)

_SUBMITTED_COMPUTED_PARAMETER_YAML = _yaml(
    """
    title: Provider lookup
    workflow_definition:
      parameters:
      - parameter_type: workflow
        workflow_parameter_type: string
        key: provider_query
        default_value: Sample Search
      blocks:
      - block_type: code
        label: search_registry
        code: |
          await page.locator("input[placeholder='Search']").fill(str(provider_query))
    """
)

_SUBMITTED_MIXED_LITERAL_YAML = _yaml(
    """
    title: Provider lookup
    workflow_definition:
      blocks:
      - block_type: code
        label: search_registry
        code: |
          await page.locator("input[placeholder='Search']").fill("Sample Search")
          await page.locator("#other").fill(provider_name)
    """
)

_SUBMITTED_TYPED_LITERAL_REWRITE_YAML = _yaml(
    """
    title: Product lookup
    workflow_definition:
      parameters:
      - {parameter_type: workflow, workflow_parameter_type: string, key: existing_filter, default_value: active}
      blocks:
      - block_type: code
        label: search_catalog
        parameter_keys: [existing_filter]
        code: |
          await page.locator("#café-search").fill("example_sku_123")
      - block_type: code
        label: select_result
        code: |
          await page.get_by_role("textbox", name="Search").type("example_sku_123")
      - block_type: loop
        label: retry_search
        loop_blocks:
        - block_type: code
          label: nested_search
          code: |
            await page.locator("#search").fill("example_sku_123")
      - block_type: code
        label: verify_cart
        code: |
          assert "example_sku_123" in await page.locator("#cart").inner_text()
    """
)


def _code_only_ctx() -> CopilotContext:
    ctx = CopilotContext(
        organization_id="o",
        workflow_id="w",
        workflow_permanent_id="wp",
        workflow_yaml="",
        browser_session_id=None,
        stream=SimpleNamespace(),  # type: ignore[arg-type]
    )
    ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
    ctx.scout_trajectory = [
        {
            "tool_name": "click",
            "selector": "#search-submit",
            "source_url": "https://example.com/search",
            "trajectory_index": 0,
        }
    ]
    return ctx


def _standard_ctx() -> CopilotContext:
    ctx = _code_only_ctx()
    ctx.block_authoring_policy = BlockAuthoringPolicy.STANDARD
    return ctx


def _draft_only_credential_ctx() -> CopilotContext:
    ctx = _code_only_ctx()
    ctx.scout_trajectory = []
    ctx.allow_untested_workflow_draft = True
    ctx.request_policy = RequestPolicy(
        testing_intent="skip_test",
        credential_input_kind="credential_name",
        credential_refs=["Saved portal credential"],
        allow_update_workflow=True,
        allow_run_blocks=False,
        allow_missing_credentials_in_draft=True,
        credential_draft_deferred_explicitly=True,
        resolved_credentials=[
            SimpleNamespace(
                credential_id="cred_missing",
                name="Saved portal credential",
                tested_url="https://example.com/login",
            )
        ],
    )
    return ctx


def _enable_imposition(ctx: CopilotContext) -> None:
    ctx.impose_synthesized_code_block = True


def _stub_successful_update(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_process_workflow_yaml(**_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            workflow_definition=SimpleNamespace(blocks=[SimpleNamespace(label="search_registry")]),
            proxy_location=None,
        )

    async def _fake_get_prior_workflow(_ctx: CopilotContext) -> None:
        return None

    monkeypatch.setattr(workflow_update_module, "_process_workflow_yaml", _fake_process_workflow_yaml)
    monkeypatch.setattr(workflow_update_module, "_get_prior_workflow", _fake_get_prior_workflow)


def _single_code_block(parsed: dict[str, object]) -> dict[str, object]:
    blocks = [block for block in workflow_blocks(parsed) if str(block.get("block_type") or "").lower() == "code"]
    assert len(blocks) == 1
    return blocks[0]


_UNREFERENCED_DEFINITION_YAML = _yaml(
    """
    title: Submit reusable service request
    workflow_definition:
      parameters:
      - {parameter_type: workflow, key: business_name, workflow_parameter_type: string}
      - {parameter_type: workflow, key: contact_email, workflow_parameter_type: string}
      - {parameter_type: workflow, key: service_address, workflow_parameter_type: string}
      - {parameter_type: workflow, key: desired_start_date, workflow_parameter_type: string}
      blocks:
      - block_type: code
        label: submit_service_request
        parameter_keys: []
        code: |
          await page.locator("#open").click()
          await page.locator("#submit").click()
    """
)


def _definition_contract_ctx() -> CopilotContext:
    ctx = _code_only_ctx()
    ctx.request_policy = RequestPolicy(
        completion_criteria=[
            CompletionCriterion(
                id="c0",
                outcome=(
                    "The workflow uses business name, contact email, service address, and desired start date "
                    "as reusable inputs."
                ),
                level="definition",
                output_path="workflow.parameters",
            )
        ]
    )
    return ctx


def _same_month_file_match_case() -> tuple[CopilotContext, str, dict[str, str]]:
    ctx = _definition_contract_ctx()
    _enable_imposition(ctx)
    source_url = "https://example.com/statements"
    ctx.scout_trajectory = [
        {
            "tool_name": "click",
            "selector": "#statement-row",
            "source_url": source_url,
            "trajectory_index": 0,
        }
    ]
    ctx.reached_download_target = ReachedDownloadTarget(
        selector='a[href="/files/invoice_100245_2026-05.pdf"]',
        affordance_text="Download invoice",
        download_kind="attribute",
        source_step="trajectory_recency",
        already_registered=False,
    )
    submitted = _yaml(
        """
        title: Download reusable invoice
        workflow_definition:
          parameters:
          - {parameter_type: workflow, key: account_number, workflow_parameter_type: string, default_value: "stale-account"}
          - {parameter_type: workflow, key: download_start_date, workflow_parameter_type: string, default_value: "2026-06-01"}
          - {parameter_type: workflow, key: download_end_date, workflow_parameter_type: string, default_value: "2026-06-30"}
          blocks:
          - block_type: code
            label: download_invoice
            parameter_keys: []
            code: |
              await page.locator("#statement-row").click()
        """
    )
    values = {
        "account_number": "100245",
        "download_start_date": "2026-05-01",
        "download_end_date": "2026-05-31",
    }
    return ctx, submitted, values


@pytest.mark.parametrize(
    "code",
    [
        'value = (await page.locator("#record").inner_text()).strip()',
        'parts = (await page.locator("#record").inner_text()).split("|")',
        'value = await page.locator("#rows").nth(2).locator("td").nth(1).inner_text()',
    ],
)
def test_browser_surface_admits_awaited_read_postprocessing_and_bounded_nth(code: str) -> None:
    mutations, unscouted, ambiguous = workflow_update_module._browser_surface_for_code(code)

    assert mutations == []
    assert unscouted == []
    assert ambiguous == []


@pytest.mark.parametrize(
    "code",
    [
        'locator = page.locator("#record")\nvalue = await locator.inner_text()',
        'read = page.locator("#record").inner_text\nvalue = await read()',
        'await getattr(page.locator("#record"), "inner_text")()',
        'await page.locator("#record").nth(-1).inner_text()',
        'await page.locator("#record").nth(10001).inner_text()',
        'for index in range(await page.locator("#rows").count()):\n    value = await page.locator("#rows").nth(index).inner_text()',
        'await page.locator("#record").fill("changed")',
        'values = sorted(await page.locator("#rows").all_text_contents(), key=page.set_default_timeout)',
    ],
)
def test_browser_surface_rejects_alias_dynamic_unbounded_and_mutation_forms(code: str) -> None:
    mutations, unscouted, ambiguous = workflow_update_module._browser_surface_for_code(code)

    assert mutations or unscouted or ambiguous


def _live_read_extraction_ctx() -> CopilotContext:
    ctx = _code_only_ctx()
    _enable_imposition(ctx)
    ctx.request_policy = RequestPolicy(
        completion_criteria=[
            CompletionCriterion(
                id="record_id",
                outcome="Record Identifier",
                output_path="output.record_id",
            )
        ]
    )
    ctx.completion_criteria_turn_state = SimpleNamespace(
        decision=SimpleNamespace(criteria=tuple(ctx.request_policy.completion_criteria))
    )
    ctx.copilot_config = CopilotConfig(requested_output_path_aliases={"record identifier": "output.record_id"})
    ctx.flow_evidence = [
        {
            "step": 2,
            "reached_via": "interaction",
            "had_bounded_schema": True,
            "evidence": {
                "source_tool": "scout_interaction",
                "interaction_tool": "click",
                "interaction_selector": "#search-submit",
                "inspection_warnings": [],
                "result_containers_truncated": False,
                "key_value_relations_truncated": False,
                "key_value_relations": [
                    {
                        "key_text": "Record Identifier",
                        "container_selector": ".record-kv",
                        "container_match_count": 1,
                        "container_position": 0,
                        "value_child_index": 1,
                        "direct_child_count": 2,
                        "visible": True,
                        "value_visible": True,
                    }
                ],
                "result_containers": [],
            },
        }
    ]
    return ctx


def _live_read_submitted_yaml() -> str:
    return _yaml(
        """
        title: Record lookup
        workflow_definition:
          blocks:
          - block_type: code
            label: extract_record
            code: |
              await page.locator("#search-submit").click()
        """
    )


def test_plan_backed_imposition_executes_generated_and_submitted_live_read_recipe() -> None:
    ctx = _live_read_extraction_ctx()
    submitted = _live_read_submitted_yaml()

    result = workflow_update_module._maybe_impose_synthesized_code_block(submitted, ctx)

    assert result.violations == []
    code = str(_single_code_block(parse_workflow_yaml(result.workflow_yaml))["code"])
    assert 'page.locator(".record-kv").nth(0)' in code
    assert code.count('return {"output": {"record_id": _extraction_value_0}}') == 1
    assert result.substitutions["extraction_candidate_source"] == "generated"
    assert result.substitutions["extraction_plan_identity"]

    submitted_result = workflow_update_module._maybe_impose_synthesized_code_block(result.workflow_yaml, ctx)

    assert submitted_result.violations == []
    assert submitted_result.substitutions["extraction_candidate_source"] == "submitted"
    submitted_code = str(_single_code_block(parse_workflow_yaml(submitted_result.workflow_yaml))["code"])
    assert submitted_code == code


def _credential_code_yaml(*, code: str, credential_id: str = "cred_missing") -> str:
    indented_code = textwrap.indent(textwrap.dedent(code).strip(), " " * 8)
    return (
        "title: Login with saved credential\n"
        "workflow_definition:\n"
        "  parameters:\n"
        "    - parameter_type: workflow\n"
        "      workflow_parameter_type: credential_id\n"
        "      key: login_credential\n"
        f"      default_value: {credential_id}\n"
        "  blocks:\n"
        "    - block_type: code\n"
        "      label: login_with_saved_credential\n"
        "      parameter_keys:\n"
        "        - login_credential\n"
        "      code: |\n"
        f"{indented_code}\n"
    )


def _directory_blocks_yaml(blocks: str) -> str:
    indented_blocks = textwrap.indent(textwrap.dedent(blocks).strip(), "  ")
    return f"title: Directory lookup\nworkflow_definition:\n  blocks:\n{indented_blocks}\n"


def _credential_fill_interaction(
    field: str,
    *,
    credential_id: str = "cred_missing",
    source_url: str = "https://authenticationtest.com/totpChallenge/",
) -> dict[str, object]:
    selectors = {
        "username": "#email",
        "password": "input[type='password']",
        "totp": "#totpmfa",
    }
    typed_lengths = {"username": 20, "password": 14, "totp": 6}
    return {
        "tool_name": "fill_credential_field",
        "selector": selectors[field],
        "source_url": source_url,
        "credential_id": credential_id,
        "credential_field": field,
        "typed_length": typed_lengths[field],
    }


def _submit_interaction(
    *,
    source_url: str = "https://authenticationtest.com/totpChallenge/",
) -> dict[str, object]:
    return {
        "tool_name": "click",
        "selector": "input[type='submit']",
        "source_url": source_url,
    }


def _terminal_metadata(label: str, declared_goal: str) -> dict:
    goal_value_paths = ["records[].number"]
    return {
        "block_label": label,
        "declared_goal": declared_goal,
        "claimed_outcomes": [
            {
                "id": f"claim:{label}",
                "scope": "outcome",
                "text": declared_goal,
                "status": "observed_not_verified",
                "goal_value_paths": goal_value_paths,
            }
        ],
        "terminal_verifier_expectations": [
            {
                "id": f"expectation:{label}",
                "text": declared_goal,
                "goal_value_paths": goal_value_paths,
            }
        ],
    }


def _stale_unresolved_repair_context() -> CodeAuthoringRepairContext:
    return CodeAuthoringRepairContext(
        block_label="stale_block",
        reason_code=SANDBOX_UNRESOLVED_NAME_REASON_CODE,
        unresolved_names=["stale_name"],
        parameter_keys=[],
    )


class TestCodeSafetySeam:
    def test_import_in_new_code_block_is_a_seam_error(self) -> None:
        errors = _code_block_safety_errors(_IMPORTING_CODE_YAML, None)
        assert len(errors) == 1
        assert "search_registry" in errors[0]
        assert "Not allowed to import modules" in errors[0]

    @pytest.mark.parametrize(
        "code",
        [
            "import requests\nawait page.goto('https://example.com')",
            "import os as json\nvalue = json",
            "import json.decoder\nvalue = 1",
            "from re import search\nmatch = search(r'x', 'x')",
        ],
    )
    def test_unsafe_import_classifications_are_seam_errors(self, code: str) -> None:
        errors = _code_block_safety_errors(_code_yaml(code), None)
        assert any("Not allowed to import modules" in str(error) for error in errors)

    def test_dunder_and_blocked_attr_use_are_seam_errors(self) -> None:
        dunder_errors = _code_block_safety_errors(_code_yaml("value = page.__class__"), None)
        assert any("private methods or attributes" in str(error) for error in dunder_errors)
        blocked_errors = _code_block_safety_errors(_code_yaml("value = page.modules"), None)
        assert any("Not allowed to access 'modules'" in str(error) for error in blocked_errors)

    def test_stripped_shim_import_keeps_name_resolvable_at_seam(self) -> None:
        sanitized, _ = strip_redundant_sandbox_imports("import json\nvalue = json.dumps({'a': 1})")
        assert _code_block_safety_errors(_code_yaml(sanitized), None) == []

    def test_unchanged_legacy_code_block_is_not_rechecked(self) -> None:
        assert _code_block_safety_errors(_IMPORTING_CODE_YAML, _IMPORTING_CODE_YAML) == []

    def test_changed_code_block_is_rechecked(self) -> None:
        assert _code_block_safety_errors(_IMPORTING_CODE_YAML, _SAFE_CODE_YAML)

    def test_safe_code_passes(self) -> None:
        assert _code_block_safety_errors(_SAFE_CODE_YAML, None) == []

    def test_unparseable_code_block_reports_one_syntax_error(self) -> None:
        errors = _code_block_safety_errors(_code_yaml("text = await page.evaluate("), None)

        assert len(errors) == 1
        assert "is not valid Python" in errors[0]

    @pytest.mark.asyncio
    async def test_denied_page_api_attribute_is_repairable_preflight_error_without_duplicate_generic_message(
        self,
    ) -> None:
        result = await _update_workflow(
            {"workflow_yaml": _code_yaml("state = await page.context.storage_state()")},
            _standard_ctx(),
        )

        assert result["ok"] is False
        joined = result["error"]
        assert "AUTHOR_PAGE_CONTEXT" in joined
        assert "failed the generated-code preflight check" in joined
        assert joined.count("failed the sandbox safety check") == 0
        assert joined.count("page.context is not allowed") == 1

    def test_unresolved_sandbox_names_are_seam_errors(self) -> None:
        errors = _code_block_safety_errors(
            _code_yaml(
                """
                raise RuntimeError("not available")
                raise ValueError("not available")
                value = getattr(page, "url", "")
                """
            ),
            None,
        )
        assert len(errors) == 1
        for expected in ("search_registry", "RuntimeError", "ValueError", "getattr", "Exception"):
            assert expected in errors[0]
        assert _code_block_safety_errors(_code_yaml('raise Exception("allowed")'), None) == []

    def test_parameter_contracts_use_block_keys_only_and_recheck_key_changes(self) -> None:
        errors = _code_block_safety_errors(_code_yaml("print(provider_query)", workflow_param=True), None)
        assert len(errors) == 1
        assert "provider_query" in errors[0]

        assert (
            _code_block_safety_errors(
                _code_yaml("print(provider_query)", parameter_keys=["provider_query"], workflow_param=True),
                None,
            )
            == []
        )

        prior = _code_yaml("print(provider_query)", parameter_keys=["provider_query"], workflow_param=True)
        current = _code_yaml("print(provider_query)", parameter_keys=[], workflow_param=True)
        errors = _code_block_safety_errors(current, prior)
        assert len(errors) == 1
        assert "provider_query" in errors[0]

    def test_branch_nested_blocks_and_existing_safety_errors_are_checked(self) -> None:
        errors = _code_block_safety_errors(_code_yaml('raise RuntimeError("nested")', nested=True), None)
        assert len(errors) == 1
        assert all(expected in errors[0] for expected in ("nested_search", "RuntimeError"))

        errors = _code_block_safety_errors(
            _code_yaml(
                """
                import asyncio
                raise RuntimeError("not available")
                """
            ),
            None,
        )
        joined = "\n".join(errors)
        assert "Not allowed to import modules" in joined
        assert "RuntimeError" in joined

    def test_name_analysis_accepts_safe_locals_and_rejects_runtime_hazards(self) -> None:
        assert (
            _code_block_safety_errors(_code_yaml('value = "row"\ncount = 1\ncount += 1\nprint(value, count)'), None)
            == []
        )

        errors = _code_block_safety_errors(
            _code_yaml(
                """
                value: str
                print(value)
                count += 1
                page = page
                x = 1
                del x
                print(x)
                if page:
                    branch_value = 1
                print(branch_value)
                class NotRunnable:
                    pass
                """
            ),
            None,
        )
        joined = "\n".join(errors)
        assert "class definitions unavailable in the code sandbox: `NotRunnable`" in joined
        for bound_somewhere in ("`count`", "`x`", "`branch_value`", "`value`"):
            assert bound_somewhere not in joined

    def test_conditionally_bound_name_is_not_a_seam_error(self) -> None:
        assert (
            _code_block_safety_errors(
                _code_yaml(
                    """
                    try:
                        count_label = await page.locator("#c").inner_text()
                    except Exception:
                        pass
                    print(count_label)
                    """
                ),
                None,
            )
            == []
        )

    def test_name_analysis_allows_recursive_helpers(self) -> None:
        assert (
            _code_block_safety_errors(
                _code_yaml(
                    """
                    def fact(n):
                        if n <= 1:
                            return 1
                        return n * fact(n - 1)

                    def is_even(n):
                        if n == 0:
                            return True
                        return is_odd(n - 1)

                    def is_odd(n):
                        if n == 0:
                            return False
                        return is_even(n - 1)

                    print(fact(4), is_even(4))
                    """
                ),
                None,
            )
            == []
        )

    def test_name_analysis_handles_try_star_branching(self) -> None:
        errors = _code_block_safety_errors(
            _code_yaml(
                """
                try:
                    risky()
                except* Exception as group:
                    recovered = group
                print(recovered)
                """
            ),
            None,
        )
        assert len(errors) == 1
        assert "risky" in errors[0]
        assert "recovered" not in errors[0]
        assert "group" not in errors[0]

    def test_syntax_error_is_a_seam_error(self) -> None:
        broken = _SAFE_CODE_YAML.replace('await page.goto("https://example.com/search")', "await page.goto(")
        errors = _code_block_safety_errors(broken, None)
        assert len(errors) == 1
        assert "not valid Python" in errors[0]

    @pytest.mark.asyncio
    async def test_update_workflow_strips_redundant_import_before_any_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        result = await _update_workflow({"workflow_yaml": _IMPORTING_CODE_YAML}, ctx)
        assert result["ok"] is True
        assert "import asyncio" not in ctx.workflow_yaml
        assert result["data"]["stripped_redundant_imports"] == ["asyncio"]

    @pytest.mark.asyncio
    async def test_update_workflow_still_rejects_third_party_import_before_any_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        result = await _update_workflow({"workflow_yaml": _REQUESTS_IMPORT_CODE_YAML}, ctx)
        assert result["ok"] is False
        assert "Not allowed to import modules" in result["error"]

    @pytest.mark.asyncio
    async def test_update_workflow_still_rejects_surface_exceeding_shim_import_before_any_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        result = await _update_workflow({"workflow_yaml": _ASYNCIO_GATHER_IMPORT_CODE_YAML}, ctx)
        assert result["ok"] is False
        assert "Not allowed to import modules" in result["error"]

    @pytest.mark.asyncio
    async def test_code_rejection_does_not_salvage_metadata_into_ctx(self) -> None:
        ctx = _code_only_ctx()
        metadata = [_terminal_metadata("search_registry", "search the registry")]
        result = await _update_workflow(
            {"workflow_yaml": _REQUESTS_IMPORT_CODE_YAML, "code_artifact_metadata": metadata}, ctx
        )
        assert result["ok"] is False
        assert ctx.code_artifact_metadata == {}

    @pytest.mark.asyncio
    async def test_code_only_unresolved_name_rejection_records_repair_context_and_accept_clears(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _code_only_ctx()

        rejected = await _update_workflow({"workflow_yaml": _code_yaml("print(provider_query)")}, ctx)

        assert rejected["ok"] is False
        repair_context = ctx.last_code_authoring_repair_context
        assert isinstance(repair_context, CodeAuthoringRepairContext)
        result_context = rejected["data"]["authoring_repair_context"]
        assert result_context["block_label"] == "search_registry"
        assert result_context["reason_code"] == SANDBOX_UNRESOLVED_NAME_REASON_CODE
        assert result_context["unresolved_names"] == ["provider_query"]
        assert result_context["parameter_keys"] == []
        assert result_context["available_parameter_keys"] == []
        assert result_context["binding_candidates"] == ["provider_query"]
        assert "page" in result_context["allowed_global_names"]
        assert "json" in result_context["allowed_global_names"]
        assert "dumps" in result_context["allowed_helper_surface"]["json"]
        assert "print(provider_query)" not in str(result_context)
        assert (
            sanitize_tool_result_for_llm("update_workflow", rejected)["data"]["authoring_repair_context"]
            == result_context
        )

        _stub_successful_update(monkeypatch)
        accepted = await _update_workflow({"workflow_yaml": _SAFE_CODE_YAML}, ctx)

        assert accepted["ok"] is True
        assert ctx.last_code_authoring_repair_context is None

    @pytest.mark.asyncio
    async def test_code_only_exact_declared_string_parameters_are_adopted_for_unresolved_names(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        submitted = _yaml(
            """
            title: Registry lookup
            workflow_definition:
              parameters:
              - {parameter_type: workflow, workflow_parameter_type: string, key: provider_query, default_value: Sample}
              - {parameter_type: workflow, workflow_parameter_type: string, key: search_location, default_value: City}
              blocks:
              - block_type: code
                label: search_registry
                code: |
                  await page.locator("#query").fill(str(provider_query))
                  await page.locator("#location").fill(str(search_location))
            """
        )

        accepted = await _update_workflow({"workflow_yaml": submitted}, ctx)

        assert accepted["ok"] is True
        parsed = parse_workflow_yaml(ctx.workflow_yaml)
        assert isinstance(parsed, dict)
        block = _single_code_block(parsed)
        assert block["parameter_keys"] == ["provider_query", "search_location"]

    @pytest.mark.asyncio
    async def test_code_only_partial_declared_parameter_name_still_rejects(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        submitted = _yaml(
            """
            title: Registry lookup
            workflow_definition:
              parameters:
              - {parameter_type: workflow, workflow_parameter_type: string, key: provider_query, default_value: Sample}
              blocks:
              - block_type: code
                label: search_registry
                code: |
                  await page.locator("#query").fill(str(provider_name))
            """
        )

        rejected = await _update_workflow({"workflow_yaml": submitted}, ctx)

        assert rejected["ok"] is False
        result_context = rejected["data"]["authoring_repair_context"]
        assert result_context["unresolved_names"] == ["provider_name"]
        assert result_context["available_parameter_keys"] == ["provider_query"]
        assert result_context["binding_candidates"] == ["provider_query", "provider_name"]

    @pytest.mark.asyncio
    async def test_unresolved_name_repair_context_includes_existing_workflow_binding_candidates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        info_calls: list[tuple[str, dict[str, str | list[str]]]] = []

        def capture_info(event: str, **kwargs: str | list[str]) -> None:
            info_calls.append((event, kwargs))

        monkeypatch.setattr(workflow_update_module.LOG, "info", capture_info)
        ctx = _code_only_ctx()
        submitted = _yaml(
            """
            title: Registry lookup
            workflow_definition:
              parameters:
              - {parameter_type: workflow, workflow_parameter_type: string, key: provider_query, default_value: Sample}
              - {parameter_type: workflow, workflow_parameter_type: string, key: search_location, default_value: City}
              - {parameter_type: workflow, workflow_parameter_type: credential_id, key: login_credential}
              blocks:
              - block_type: code
                label: search_registry
                code: |
                  print(provider_name)
            """
        )

        rejected = await _update_workflow({"workflow_yaml": submitted}, ctx)

        assert rejected["ok"] is False
        repair_context = rejected["data"]["authoring_repair_context"]
        assert repair_context["reason_code"] == SANDBOX_UNRESOLVED_NAME_REASON_CODE
        assert repair_context["unresolved_names"] == ["provider_name"]
        assert repair_context["parameter_keys"] == []
        assert repair_context["available_parameter_keys"] == ["provider_query", "search_location"]
        assert repair_context["binding_candidates"] == ["provider_query", "search_location", "provider_name"]
        assert "login_credential" not in str(repair_context)
        assert (
            "copilot code authoring repair context stored",
            {
                "reason_code": SANDBOX_UNRESOLVED_NAME_REASON_CODE,
                "block_label": "search_registry",
                "unresolved_names": ["provider_name"],
                "parameter_keys": [],
                "available_parameter_keys": ["provider_query", "search_location"],
                "binding_candidates": ["provider_query", "search_location", "provider_name"],
            },
        ) in info_calls

    @pytest.mark.asyncio
    async def test_standard_policy_unresolved_name_rejection_has_no_repair_context(self) -> None:
        ctx = _standard_ctx()

        result = await _update_workflow({"workflow_yaml": _code_yaml("print(provider_query)")}, ctx)

        assert result["ok"] is False
        assert "authoring_repair_context" not in result["data"]
        assert ctx.last_code_authoring_repair_context is None

    @pytest.mark.parametrize(
        "workflow_yaml",
        [
            _SAFE_CODE_YAML.replace('await page.goto("https://example.com/search")', "await page.goto("),
            _REQUESTS_IMPORT_CODE_YAML,
            _code_yaml('await page.evaluate("1 + 1")'),
            _code_yaml("""await page.wait_for_function("document.body.innerText.includes('Submitted')")"""),
        ],
    )
    @pytest.mark.asyncio
    async def test_non_name_authoring_rejects_do_not_carry_repair_context(self, workflow_yaml: str) -> None:
        ctx = _code_only_ctx()

        result = await _update_workflow({"workflow_yaml": workflow_yaml}, ctx)

        assert result["ok"] is False
        # A block carrying no data omits the key entirely, which satisfies the same contract.
        assert "authoring_repair_context" not in result.get("data", {})
        assert ctx.last_code_authoring_repair_context is None

    @pytest.mark.parametrize(
        "workflow_yaml",
        [
            _code_yaml("import os\nprint(provider_query)"),
            _code_yaml("await page.request.get(provider_query)"),
        ],
    )
    @pytest.mark.asyncio
    async def test_mixed_primary_authoring_rejects_do_not_carry_repair_context(self, workflow_yaml: str) -> None:
        ctx = _code_only_ctx()

        result = await _update_workflow({"workflow_yaml": workflow_yaml}, ctx)

        assert result["ok"] is False
        assert "authoring_repair_context" not in result["data"]
        assert ctx.last_code_authoring_repair_context is None

    def test_ambiguous_selector_repair_context_only_carries_valid_refiner(self) -> None:
        code_block = {"label": "order_status"}
        dropped = {
            "reason_code": "ambiguous_bare_selector",
            "selector": "button",
            "trajectory_index": 0,
        }
        scout_trajectory = [
            {"tool_name": "click", "selector": "button", "source_url": _RESALE_URL, "trajectory_index": 0},
            {
                "tool_name": "click",
                "selector": "button:nth-of-type(2)",
                "source_url": _RESALE_URL,
                "trajectory_index": 1,
            },
        ]

        repair_context = workflow_update_module._ambiguous_bare_selector_repair_context(
            code_block=code_block,
            dropped=dropped,
            scout_trajectory=scout_trajectory,
        )

        assert isinstance(repair_context, CodeAuthoringRepairContext)
        assert repair_context.refiner_selector is None
        assert repair_context.selector_alternatives == []

        scout_trajectory.append(
            {
                "tool_name": "click",
                "selector": 'button[data-action="status"]',
                "source_url": _RESALE_URL,
                "trajectory_index": 2,
            }
        )
        repair_context = workflow_update_module._ambiguous_bare_selector_repair_context(
            code_block=code_block,
            dropped=dropped,
            scout_trajectory=scout_trajectory,
        )

        assert isinstance(repair_context, CodeAuthoringRepairContext)
        assert repair_context.refiner_selector == 'button[data-action="status"]'
        assert repair_context.selector_alternatives == []

    def test_ambiguous_selector_repair_context_carries_sanitized_same_page_alternatives(self) -> None:
        code_block = {"label": "order_status"}
        private_url = "https://example.com/orders?session=secret-token#account"
        dropped = {
            "reason_code": "ambiguous_bare_selector",
            "selector": "button",
            "trajectory_index": 1,
        }
        scout_trajectory = [
            {
                "tool_name": "type_text",
                "selector": "#order-id",
                "source_url": private_url,
                "role": "textbox",
                "trajectory_index": 0,
            },
            {"tool_name": "click", "selector": "button", "source_url": private_url, "trajectory_index": 1},
            {
                "tool_name": "click",
                "selector": "button:nth-of-type(2)",
                "source_url": private_url,
                "role": "button",
                "trajectory_index": 2,
            },
            {
                "tool_name": "click",
                "selector": 'role=button[name="Order status"]',
                "source_url": private_url,
                "role": "button",
                "trajectory_index": 3,
            },
            {
                "tool_name": "hover",
                "selector": "#order-total",
                "source_url": private_url,
                "role": "status",
                "trajectory_index": 4,
            },
            {
                "tool_name": "click",
                "selector": '[data-action="other-page"]',
                "source_url": "https://example.com/account",
                "role": "button",
                "trajectory_index": 5,
            },
        ]

        repair_context = workflow_update_module._ambiguous_bare_selector_repair_context(
            code_block=code_block,
            dropped=dropped,
            scout_trajectory=scout_trajectory,
        )

        assert isinstance(repair_context, CodeAuthoringRepairContext)
        assert repair_context.source_url == "https://example.com"
        assert repair_context.refiner_selector is None
        assert repair_context.selector_alternatives == [
            {"tool_name": "type_text", "role": "textbox", "selector": "#order-id"},
            {"tool_name": "click", "role": "button", "selector": 'role=button[name="Order status"]'},
            {"tool_name": "hover", "role": "status", "selector": "#order-total"},
        ]
        dumped = repair_context.model_dump(mode="json")
        assert "secret-token" not in str(dumped)
        assert "button:nth-of-type" not in str(dumped)
        assert "other-page" not in str(dumped)


def _live_root_value_bearing_ctx() -> CopilotContext:
    ctx = _code_only_ctx()
    ctx.turn_id = "t-live-root-value-bearing-directive"
    ctx.scout_trajectory = []
    ctx.request_policy = RequestPolicy(
        request_slot_failure_kind="invalid_anchor_correction",
        completion_criteria=[
            CompletionCriterion(
                id="3362dc25307750ce8cfcc05e3459f621874ba48d0a4500fbdd67f95d84cace37",
                outcome="A visible manual-service path is returned as a blocker.",
                output_path="output.blocker",
                contingent_on="the site exposes only a manual-service path",
                contingent_antecedent_output_path="output.blocker",
                antecedent_family="blocker",
            ),
            *[
                CompletionCriterion(
                    id=f"slot_{index}",
                    outcome=f"The returned record includes slot {index}.",
                    output_path="output",
                    antecedent_family="undecidable",
                    request_slot_id=f"slot_{index}",
                    pinability="unpinnable",
                    mint_disposition="degraded",
                    mint_degrade="undecidable_judgment",
                    requested_output_floor_rekeyed=True,
                    floor_rekeyed_from_path=path,
                )
                for index, path in enumerate(
                    (
                        "output.confirmation_number",
                        "output.account_number",
                        "output.selected_start_date",
                        "output.deposit_amount",
                        "output.next_owner",
                    )
                )
            ],
        ],
    )
    return ctx


class TestCodeRepairProgressClassification:
    def test_output_safety_clamp_byte_parity(self) -> None:
        source = inspect.getsource(workflow_update_module._extraction_code_with_required_static_return)

        assert hashlib.sha256(source.encode()).hexdigest() == (
            "32d1a9443d6dae7e8f6367cbe52902d7aaeb270e78c0275a66648d6013f24f9d"
        )

    def test_recut_binding_is_inert_for_runtime_self_heal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx = _definition_contract_ctx()
        _enable_imposition(ctx)
        ctx.turn_origin = TurnOrigin.runtime_self_heal
        ctx.authoring_parameter_binding_snapshot = Mock()
        monkeypatch.setattr(
            workflow_update_module,
            "_authoring_parameter_binding_resolution",
            Mock(side_effect=AssertionError("runtime self-heal must not resolve authoring bindings")),
        )

        result = workflow_update_module._maybe_impose_synthesized_code_block(
            _UNREFERENCED_DEFINITION_YAML,
            ctx,
            runtime_parameters={"business_name": "Example Co"},
        )

        assert result.workflow_yaml == _UNREFERENCED_DEFINITION_YAML
        assert result.violations == []
        assert result.substitutions is None
        assert ctx.authoring_parameter_binding_snapshot is None

    def test_same_month_file_match_rebinds_literal_when_other_block_references_all_keys(self) -> None:
        ctx, _, values = _same_month_file_match_case()
        submitted = _yaml(
            """
            title: Download reusable invoice
            workflow_definition:
              parameters:
              - {parameter_type: workflow, key: account_number, workflow_parameter_type: string, default_value: "100245"}
              - {parameter_type: workflow, key: download_start_date, workflow_parameter_type: string, default_value: "2026-05-01"}
              - {parameter_type: workflow, key: download_end_date, workflow_parameter_type: string, default_value: "2026-05-31"}
              blocks:
              - block_type: text_prompt
                label: prepare_invoice_request
                parameter_keys: [account_number, download_start_date, download_end_date]
                prompt: "Inputs: {{ parameters.account_number }}, {{ parameters.download_start_date }}, {{ parameters.download_end_date }}"
              - block_type: code
                label: download_invoice
                parameter_keys: []
                code: |
                  await page.locator("#statement-row").click()
                  await page.locator('a[href="/files/invoice_100245_2026-05.pdf"]').click()
            """
        )
        assert workflow_update_module._definition_plane_preflight_reject(ctx, submitted) is None

        with capture_logs() as logs:
            result = workflow_update_module._maybe_impose_synthesized_code_block(
                submitted,
                ctx,
                runtime_parameters=values,
            )

        assert result.violations == []
        parsed = parse_workflow_yaml(result.workflow_yaml)
        assert isinstance(parsed, dict)
        code = str(_single_code_block(parsed)["code"])
        assert "100245" not in code
        assert "2026-05" not in code
        assert any(
            log.get("event") == "copilot_spine_same_month_file_match_transform_applied"
            and log.get("provenance_source") == "same_month_file_match"
            for log in logs
        )

    def test_same_month_file_match_rejects_stale_pending_directive_before_persistence(self) -> None:
        ctx, submitted, values = _same_month_file_match_case()
        keys = tuple(values)
        directive = build_authoring_parameter_binding_directive(
            structural_key="stale-structural-key",
            source_origin="https://example.com",
            candidates=[AuthoringParameterBindingCandidate(declared_key="account_number", field_selector="#account")],
        )
        ctx.last_code_authoring_repair_context = CodeAuthoringRepairContext(
            block_label="download_invoice",
            reason_code="synthesized_parameter_binding_ambiguous",
            unresolved_names=list(keys),
            parameter_keys=list(keys),
            parameter_binding_directive=directive,
        )

        result = workflow_update_module._maybe_impose_synthesized_code_block(
            submitted,
            ctx,
            runtime_parameters=values,
        )

        assert result.workflow_yaml == submitted
        assert result.violations == [
            "Unable to impose synthesized code block: stored parameter binding directive is stale."
        ]
        assert result.repair_context is ctx.last_code_authoring_repair_context

    def test_same_month_file_match_persistence_provenance_rejects_missing_identity_hole(self) -> None:
        ctx, submitted, values = _same_month_file_match_case()
        result = workflow_update_module._maybe_impose_synthesized_code_block(
            submitted,
            ctx,
            runtime_parameters=values,
        )
        assert result.violations == []
        synthesized = ctx.imposition_synthesized_block
        assert synthesized is not None
        provenance = next(
            record
            for record in synthesized.diagnostics.locator_provenance
            if record.get("source") == code_block_synthesis_module.SAME_MONTH_FILE_MATCH_PROVENANCE_SOURCE
        )
        tampered = dict(provenance)
        tampered["holes"] = [hole for hole in provenance["holes"] if hole["format_id"] != "identity"]

        assert workflow_update_module._locator_provenance_is_self_validating(provenance)
        assert not workflow_update_module._locator_provenance_is_self_validating(tampered)

    def test_same_month_file_match_rejects_ambiguous_fresh_directive_before_persistence(self) -> None:
        ctx, submitted, values = _same_month_file_match_case()
        source_url = "https://example.com/statements"
        ctx.scout_trajectory.append(
            {
                "tool_name": "click",
                "selector": "#submit",
                "source_url": source_url,
                "trajectory_index": 1,
            }
        )
        ctx.composition_page_evidence = {
            "source_tool": "inspect_page_for_composition",
            "current_url": source_url,
            "forms": [
                {
                    "fields": [{"selector": "#account", "value": values["account_number"]}],
                    "submit_controls": [{"selector": "#submit"}],
                }
            ],
        }

        result = workflow_update_module._maybe_impose_synthesized_code_block(
            submitted,
            ctx,
            runtime_parameters=values,
        )

        assert result.workflow_yaml == submitted
        assert result.violations == [
            "Unable to impose synthesized code block: current-page parameter binding is ambiguous."
        ]
        assert result.repair_context is not None
        assert result.repair_context.reason_code == "synthesized_parameter_binding_ambiguous"
        assert result.repair_context.parameter_binding_directive is not None
        assert result.repair_context.parameter_binding_directive.source_origin == "https://example.com"

    def test_selection_resolution_binds_witnessed_click_value(self) -> None:
        ctx = SimpleNamespace(
            scout_trajectory=[
                {
                    "tool_name": "click",
                    "selector": '[data-account="100245"]',
                    "source_url": "https://example.com/statements",
                    "trajectory_index": 0,
                }
            ]
        )
        for interaction in ctx.scout_trajectory:
            interaction["input_correspondences"] = code_block_synthesis_module.input_correspondences_for_interaction(
                interaction, {"account_number": "100245"}
            )
        resolution = workflow_update_module._selection_parameter_binding_resolution(
            ctx,
            target_keys=["account_number"],
            ephemeral_values={"account_number": "100245"},
            structural_key="definition-reject",
            source_origin="https://example.com",
        )
        assert resolution.snapshot is not None
        binding = resolution.snapshot.field_bindings[0]
        assert binding.declared_key == "account_number"
        assert binding.match_basis == "scouted_selection_value"

    def test_selection_resolution_rejects_prose_echo_without_scouted_interaction(self) -> None:
        ctx = SimpleNamespace(
            scout_trajectory=[
                {
                    "tool_name": "click",
                    "selector": "#unrelated-open-button",
                    "source_url": "https://example.com/statements",
                    "trajectory_index": 0,
                }
            ]
        )
        for interaction in ctx.scout_trajectory:
            interaction["input_correspondences"] = code_block_synthesis_module.input_correspondences_for_interaction(
                interaction, {"account_number": "100245"}
            )
        resolution = workflow_update_module._selection_parameter_binding_resolution(
            ctx,
            target_keys=["account_number"],
            ephemeral_values={"account_number": "100245"},
            structural_key="definition-reject",
            source_origin="https://example.com",
        )
        assert resolution.snapshot is None
        assert resolution.directive is None

    def test_selection_resolution_short_value_below_witness_bar_does_not_bind(self) -> None:
        ctx = SimpleNamespace(
            scout_trajectory=[
                {
                    "tool_name": "select_option",
                    "selector": "#plan",
                    "value": "US",
                    "source_url": "https://example.com/statements",
                    "trajectory_index": 0,
                }
            ]
        )
        resolution = workflow_update_module._selection_parameter_binding_resolution(
            ctx,
            target_keys=["region"],
            ephemeral_values={"region": "US"},
            structural_key="definition-reject",
            source_origin="https://example.com",
        )
        assert resolution.snapshot is None
        assert resolution.directive is None

    def test_selection_resolution_multiple_matches_yield_candidates_without_snapshot(self) -> None:
        trajectory = [
            {
                "tool_name": "click",
                "selector": '[data-account="100245"]',
                "source_url": "https://example.com/statements",
                "trajectory_index": 0,
            },
            {
                "tool_name": "click",
                "selector": '[data-ref="100245"]',
                "source_url": "https://example.com/statements",
                "trajectory_index": 1,
            },
        ]
        for interaction in trajectory:
            interaction["input_correspondences"] = code_block_synthesis_module.input_correspondences_for_interaction(
                interaction, {"account_number": "100245"}
            )
        ctx = SimpleNamespace(scout_trajectory=trajectory)
        resolution = workflow_update_module._selection_parameter_binding_resolution(
            ctx,
            target_keys=["account_number"],
            ephemeral_values={"account_number": "100245"},
            structural_key="definition-reject",
            source_origin="https://example.com",
        )
        assert resolution.snapshot is None
        assert resolution.directive is not None

    def test_selection_resolution_cross_origin_click_is_not_admitted(self) -> None:
        trajectory = [
            {
                "tool_name": "click",
                "selector": '[data-account="100245"]',
                "source_url": "https://other.example.org/statements",
                "trajectory_index": 0,
            }
        ]
        for interaction in trajectory:
            interaction["input_correspondences"] = code_block_synthesis_module.input_correspondences_for_interaction(
                interaction, {"account_number": "100245"}
            )
        ctx = SimpleNamespace(scout_trajectory=trajectory)
        resolution = workflow_update_module._selection_parameter_binding_resolution(
            ctx,
            target_keys=["account_number"],
            ephemeral_values={"account_number": "100245"},
            structural_key="definition-reject",
            source_origin="https://example.com",
        )
        assert resolution.snapshot is None
        assert resolution.directive is None

    @pytest.mark.asyncio
    async def test_synthesized_business_input_floor_allows_fully_bound_draft(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        ctx.request_policy = RequestPolicy(
            completion_criteria=[
                CompletionCriterion(
                    id="submit-request",
                    outcome="the service request is submitted",
                    kind="terminal_action",
                    terminal_action_family="request",
                    level="run",
                )
            ]
        )
        ctx.synthesized_block_offered = True
        ctx.spine_imposition_owned_attempt = True
        bound_yaml = _yaml(
            """
            title: Submit reusable service request
            workflow_definition:
              parameters:
              - {parameter_type: workflow, key: business_name, workflow_parameter_type: string}
              - {parameter_type: workflow, key: service_address, workflow_parameter_type: string}
              blocks:
              - block_type: code
                label: submit_service_request
                parameter_keys: [business_name, service_address]
                code: |
                  await page.locator("#business").fill(str(business_name))
                  await page.locator("#address").fill(str(service_address))
                  await page.locator("#submit").click()
            """
        )
        monkeypatch.setattr(
            workflow_update_module,
            "_maybe_impose_synthesized_code_block",
            lambda *_args, **_kwargs: workflow_update_module._SynthesizedCodeImpositionResult(
                workflow_yaml=bound_yaml,
                substitutions={"submit_service_request": "synthesized"},
            ),
        )

        result = await _update_workflow(
            {"workflow_yaml": bound_yaml},
            ctx,
            allow_missing_credentials=True,
        )

        assert result["ok"] is True
        assert ctx.workflow_yaml == bound_yaml

    @pytest.mark.asyncio
    async def test_bound_draft_run_blocks_dispatches_when_criteria_dark(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        ctx.request_policy = RequestPolicy(allow_run_blocks=True)
        bound_yaml = _yaml(
            """
            title: Download monthly statement
            workflow_definition:
              parameters:
              - {parameter_type: workflow, key: account_number, workflow_parameter_type: string}
              blocks:
              - block_type: code
                label: fetch_invoice_pdf
                parameter_keys: [account_number]
                code: |
                  await page.locator("#account").fill(str(account_number))
                  await page.locator("#current-statement-row").click()
            """
        )

        persist_result = await _update_workflow(
            {"workflow_yaml": bound_yaml},
            ctx,
            allow_missing_credentials=True,
        )
        assert persist_result["ok"] is True

        dispatched: list[object] = []

        async def _run(_params: object, _ctx: object, **_kwargs: object) -> dict[str, object]:
            dispatched.append(_params)
            return {"ok": True, "run_status": "completed"}

        monkeypatch.setattr(tools_module, "_run_blocks_and_collect_debug", _run)
        monkeypatch.setattr(tools_module, "_authority_tool_error", lambda *_args, **_kwargs: None)
        await tools_module.run_blocks_tool.on_invoke_tool(
            SimpleNamespace(context=ctx, tool_name="run_blocks_and_collect_debug"),
            json.dumps({"block_labels": ["fetch_invoice_pdf"]}),
        )

        assert dispatched != []

    @pytest.mark.asyncio
    async def test_public_update_and_run_consumes_existing_page_binding_before_definition_reject(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _definition_contract_ctx()
        _enable_imposition(ctx)
        monkeypatch.setattr(tools_module, "_request_policy_allows_update_and_skip_run", lambda *_args: False)
        monkeypatch.setattr(tools_module, "_authority_tool_error", lambda *_args, **_kwargs: None)
        source_url = "https://example.com/utility"
        runtime_parameters = {
            "business_name": "Example Co",
            "contact_email": "ops@example.com",
            "service_address": "1 Example Way",
            "desired_start_date": "2026-08-01",
        }
        ctx.scout_trajectory = [
            {
                "tool_name": "type_text",
                "selector": "#company",
                "source_url": source_url,
                "typed_length": 10,
                "trajectory_index": 0,
            },
            {
                "tool_name": "type_text",
                "selector": "#email",
                "source_url": source_url,
                "typed_length": 15,
                "trajectory_index": 1,
            },
            {"tool_name": "click", "selector": "#submit", "source_url": source_url, "trajectory_index": 2},
        ]
        ctx.composition_page_evidence = {
            "source_tool": "inspect_page_for_composition",
            "current_url": source_url,
            "forms": [
                {
                    "fields": [
                        {"selector": "#company", "value": runtime_parameters["business_name"]},
                        {"selector": "#email", "value": runtime_parameters["contact_email"]},
                        {"selector": "#address", "value": runtime_parameters["service_address"]},
                        {"selector": "#date", "value": runtime_parameters["desired_start_date"]},
                    ],
                    "submit_controls": [{"selector": "#submit"}],
                }
            ],
        }

        async def _no_prior_definition(_ctx: CopilotContext) -> None:
            return None

        dispatched_candidates: list[str] = []

        async def _run(_params: object, run_ctx: CopilotContext, **_kwargs: object) -> dict[str, object]:
            dispatched_candidates.append(run_ctx.workflow_yaml or "")
            return {"ok": False, "error": "stop after exact candidate"}

        async def _verification(*_args: object, **_kwargs: object) -> None:
            return None

        monkeypatch.setattr(tools_module, "_get_prior_workflow_definition", _no_prior_definition)
        monkeypatch.setattr(tools_module, "_plan_frontier", lambda *_args: (["submit_service_request"], {}, None))
        monkeypatch.setattr(tools_module, "_frontier_run_size_error", lambda *_args: None)
        monkeypatch.setattr(tools_module, "_run_blocks_and_collect_debug", _run)
        monkeypatch.setattr(tools_module, "_verify_and_record_run_blocks_result", _verification)
        monkeypatch.setattr(tools_module, "_tool_visible_result_after_completion_verification", lambda _c, r, _v: r)

        with capture_logs() as logs:
            result = await tools_module.update_and_run_blocks_tool.on_invoke_tool(
                SimpleNamespace(context=ctx, tool_name="update_and_run_blocks"),
                json.dumps(
                    {
                        "workflow_yaml": _UNREFERENCED_DEFINITION_YAML,
                        "block_labels": ["submit_service_request"],
                        "parameters": runtime_parameters,
                    }
                ),
            )

        bound = next(log for log in logs if log["event"] == "copilot recorded outcome submit rung bound")
        consumed = next(
            log for log in logs if log["event"] == "copilot recorded outcome binding consumed by synthesizer"
        )
        assert consumed["binding_fingerprints"] == [bound["binding_fingerprint"]]
        assert dispatched_candidates == [ctx.workflow_yaml]
        assert json.loads(result)["error"] == "stop after exact candidate"
        parsed = parse_workflow_yaml(ctx.workflow_yaml)
        block = _single_code_block(parsed)
        code = str(block["code"])
        assert block["parameter_keys"] == [
            "business_name",
            "contact_email",
            "desired_start_date",
            "service_address",
        ]
        assert code.count('page.locator("#company").fill(str(business_name))') == 1
        assert code.count('page.locator("#email").fill(str(contact_email))') == 1
        assert code.count('page.locator("#address").fill(str(service_address))') == 1
        assert code.count('page.locator("#date").fill(str(desired_start_date))') == 1
        assert workflow_update_module._definition_plane_preflight_reject(ctx, ctx.workflow_yaml) is None

    @pytest.mark.asyncio
    async def test_public_initial_preflight_preserves_code_safety_precedence_for_untagged_inputs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _code_only_ctx()
        ctx.request_policy = RequestPolicy(
            completion_criteria=[CompletionCriterion(id="run", outcome="The service request is submitted.")]
        )
        unsafe_yaml = _UNREFERENCED_DEFINITION_YAML.replace(
            'await page.locator("#open").click()',
            'import requests\n      await page.locator("#open").click()',
        )
        monkeypatch.setattr(tools_module, "_request_policy_allows_update_and_skip_run", lambda *_args: False)
        monkeypatch.setattr(tools_module, "_authority_tool_error", lambda *_args, **_kwargs: None)

        result = await tools_module.update_and_run_blocks_tool.on_invoke_tool(
            SimpleNamespace(context=ctx, tool_name="update_and_run_blocks"),
            json.dumps({"workflow_yaml": unsafe_yaml, "block_labels": ["submit_service_request"]}),
        )

        parsed = json.loads(result)
        assert parsed["ok"] is False
        assert parsed.get("data", {}).get("reason_code") != "definition_contract_unsatisfied"
        assert "sandbox safety check" in parsed["error"]

    def test_definition_preflight_drops_bindings_from_non_fallthrough_branch(self) -> None:
        ctx = _definition_contract_ctx()
        terminal_branch_yaml = _yaml(
            """
            title: Submit reusable service request
            workflow_definition:
              parameters:
              - {parameter_type: workflow, key: business_name, workflow_parameter_type: string}
              blocks:
              - block_type: code
                label: submit_service_request
                parameter_keys: [business_name]
                code: |
                  if page.url.endswith("/done"):
                      company = business_name
                      if True:
                          return {"already_done": True}
                  else:
                      company = "fixed"
                  await page.locator("#company").fill(company)
            """
        )

        result = workflow_update_module._definition_plane_preflight_reject(ctx, terminal_branch_yaml)

        assert result is not None
        assert result.unreferenced_parameter_keys == ("business_name",)

    @pytest.mark.parametrize(
        "terminal_test", ["if 1:\n    return {'already_done': True}", "while True:\n    return {'already_done': True}"]
    )
    def test_definition_preflight_drops_bindings_from_constant_terminal_control(self, terminal_test: str) -> None:
        ctx = _definition_contract_ctx()
        indented_terminal = textwrap.indent(terminal_test, "          ")
        workflow_yaml = _yaml(
            "title: Submit reusable service request\n"
            "workflow_definition:\n"
            "  parameters:\n"
            "  - {parameter_type: workflow, key: business_name, workflow_parameter_type: string}\n"
            "  blocks:\n"
            "  - block_type: code\n"
            "    label: submit_service_request\n"
            "    parameter_keys: [business_name]\n"
            "    code: |\n"
            "      if page.url.endswith('/done'):\n"
            "          company = business_name\n"
            f"{indented_terminal}\n"
            "      else:\n"
            "          company = 'fixed'\n"
            "      await page.locator('#company').fill(company)\n"
        )

        result = workflow_update_module._definition_plane_preflight_reject(ctx, workflow_yaml)

        assert result is not None
        assert result.unreferenced_parameter_keys == ("business_name",)

    def test_definition_preflight_ignores_break_in_unreachable_constant_branch(self) -> None:
        ctx = _definition_contract_ctx()
        workflow_yaml = _yaml(
            """
            title: Submit reusable service request
            workflow_definition:
              parameters:
              - {parameter_type: workflow, key: business_name, workflow_parameter_type: string}
              blocks:
              - block_type: code
                label: submit_service_request
                parameter_keys: [business_name]
                code: |
                  if page.url.endswith("/done"):
                      company = business_name
                      while True:
                          if False:
                              break
                          return {"already_done": True}
                  else:
                      company = "fixed"
                  await page.locator("#company").fill(company)
            """
        )

        result = workflow_update_module._definition_plane_preflight_reject(ctx, workflow_yaml)

        assert result is not None
        assert result.unreferenced_parameter_keys == ("business_name",)

    def test_definition_preflight_ignores_break_overridden_by_terminal_finally(self) -> None:
        ctx = _definition_contract_ctx()
        workflow_yaml = _yaml(
            """
            title: Submit reusable service request
            workflow_definition:
              parameters:
              - {parameter_type: workflow, key: business_name, workflow_parameter_type: string}
              blocks:
              - block_type: code
                label: submit_service_request
                parameter_keys: [business_name]
                code: |
                  if page.url.endswith("/done"):
                      company = business_name
                      while True:
                          try:
                              break
                          finally:
                              return {"already_done": True}
                  else:
                      company = "fixed"
                  await page.locator("#company").fill(company)
            """
        )

        result = workflow_update_module._definition_plane_preflight_reject(ctx, workflow_yaml)

        assert result is not None
        assert result.unreferenced_parameter_keys == ("business_name",)

    @pytest.mark.parametrize(
        ("code", "expected_unreferenced"),
        [
            (
                "values = [business_name for business_name in ['fixed']]\n"
                "await page.locator('#company').fill(values[0])",
                ["business_name"],
            ),
            (
                "async def fill_company():\n"
                "    await page.locator('#company').fill(str(business_name))\n"
                "await fill_company()",
                [],
            ),
        ],
    )
    def test_definition_preflight_honors_python_scopes(self, code: str, expected_unreferenced: list[str]) -> None:
        ctx = _definition_contract_ctx()
        indented_code = textwrap.indent(code, "      ")
        scoped_yaml = _yaml(
            "title: Submit reusable service request\n"
            "workflow_definition:\n"
            "  parameters:\n"
            "  - {parameter_type: workflow, key: business_name, workflow_parameter_type: string}\n"
            "  blocks:\n"
            "  - block_type: code\n"
            "    label: submit_service_request\n"
            "    parameter_keys: [business_name]\n"
            "    code: |\n"
            f"{indented_code}\n"
        )

        result = workflow_update_module._definition_plane_preflight_reject(ctx, scoped_yaml)

        if expected_unreferenced:
            assert result is not None
            assert list(result.unreferenced_parameter_keys) == expected_unreferenced
        else:
            assert result is None

    @pytest.mark.parametrize(
        ("template", "is_consumed"),
        [
            ('{{ "business_name" }}', False),
            ("{% if business_name %}/customer{% endif %}", True),
        ],
    )
    def test_definition_preflight_uses_jinja_semantics(self, template: str, is_consumed: bool) -> None:
        ctx = _definition_contract_ctx()
        templated_yaml = _yaml(
            f"""
            title: Submit reusable service request
            workflow_definition:
              parameters:
              - {{parameter_type: workflow, key: business_name, workflow_parameter_type: string}}
              blocks:
              - block_type: navigation
                label: open_customer
                url: {json.dumps(template)}
            """
        )

        result = workflow_update_module._definition_plane_preflight_reject(ctx, templated_yaml)

        assert (result is None) is is_consumed

    @pytest.mark.asyncio
    async def test_definition_preflight_defers_to_repairable_syntax_error(self) -> None:
        ctx = _definition_contract_ctx()
        malformed_yaml = _yaml(
            """
            title: Submit reusable service request
            workflow_definition:
              parameters:
              - {parameter_type: workflow, key: business_name, workflow_parameter_type: string}
              blocks:
              - block_type: code
                label: submit_service_request
                parameter_keys: [business_name]
                code: |
                  await page.locator("#company").fill(str(business_name)
            """
        )

        result = await _update_workflow({"workflow_yaml": malformed_yaml}, ctx)

        assert result["ok"] is False
        assert "not valid Python" in result["error"]
        assert result.get("data", {}).get("reason_code") != "definition_contract_unsatisfied"

    @pytest.mark.asyncio
    async def test_code_safety_seam_reject_carries_progress_surface_kind(self) -> None:
        ctx = _code_only_ctx()
        result = await _update_workflow({"workflow_yaml": _REQUESTS_IMPORT_CODE_YAML}, ctx)
        assert result["ok"] is False
        assert result["data"]["surface_kind"] == "code_repair_progress"
        assert result["data"]["progress_text"]
        # The substantive copy is unchanged; the progress text is a separate carrier.
        assert result["user_facing_summary"] == (
            "I need to adjust the workflow's code so it can run safely before testing."
        )
        assert result["data"]["progress_text"] != result["user_facing_summary"]

    @pytest.mark.asyncio
    async def test_generated_code_preflight_string_reject_is_not_authoritative(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            workflow_update_module,
            "_code_block_safety_errors",
            lambda workflow_yaml, prior_yaml: [
                "Code block `search_registry` failed the generated-code preflight check: "
                "AUTHOR_PAGE_REQUEST: page.request is not allowed."
            ],
        )
        ctx = _code_only_ctx()
        result = await _update_workflow({"workflow_yaml": _SAFE_CODE_YAML}, ctx)

        assert result["ok"] is False
        assert ctx.latest_recorded_build_test_outcome is not None
        assert ctx.latest_recorded_build_test_outcome.reason_code == "code_safety_reject"
        assert ctx.latest_recorded_build_test_outcome.is_authoritative is False
        assert ctx.latest_recorded_build_test_outcome.structural_key is None

    def test_code_safety_payload_keeps_typed_security_error_authoritative(self) -> None:
        payload = _code_safety_reject_payload(
            [
                CodeBlockSecurityError(
                    "Code block `search_registry` failed the Copilot code security check: page.request is not allowed.",
                    block_label="search_registry",
                    reason_code="AUTHOR_PAGE_REQUEST",
                    surface="page.request",
                )
            ]
        )

        assert payload == {
            "code_safety_errors": [
                {
                    "block_label": "search_registry",
                    "reason_code": "AUTHOR_PAGE_REQUEST",
                    "surface": "page.request",
                }
            ]
        }

    def test_code_safety_payload_rejects_string_only_preflight_authority(self) -> None:
        payload = _code_safety_reject_payload(
            [
                "Code block `search_registry` failed the generated-code preflight check: "
                "AUTHOR_PAGE_REQUEST: page.request is not allowed."
            ]
        )

        assert payload is None

    @pytest.mark.asyncio
    async def test_authoritative_recorded_outcome_allows_changed_authored_candidate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        signature = authored_structure_signature_from_workflow(_SAFE_CODE_YAML)
        changed_yaml = _code_yaml('await page.goto("https://example.com/search")\nvalue = "changed"')
        ctx.latest_recorded_build_test_outcome = RecordedBuildTestOutcome(
            phase="persisted_block_run",
            attempted_tool="update_and_run_blocks",
            verdict="repairable_failure",
            reason_code="outcome_not_demonstrated",
            structural_failure_identity="completion:typed-outcome",
            authored_structure_signature=signature,
        )

        result = await _update_workflow({"workflow_yaml": changed_yaml}, ctx, allow_missing_credentials=True)

        assert result["ok"] is True
        assert ctx.workflow_yaml == changed_yaml
        assert ctx.has_staged_proposal is True

    @pytest.mark.asyncio
    async def test_authoritative_outcome_not_demonstrated_allows_implicit_keyed_output_candidate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        label = "lookup_provider_and_extract_credentials"
        ctx.completion_verification_result = CompletionVerificationResult(
            status="evaluated",
            criterion_ids=["npi", "locations"],
            verdicts=[
                CriterionVerdict(criterion_id="npi", state="unsatisfied", reason_code="no_evidence"),
                CriterionVerdict(criterion_id="locations", state="unsatisfied", reason_code="no_evidence"),
            ],
        )
        ctx.code_artifact_metadata = {
            label: {
                "block_label": label,
                "claimed_outcomes": [{"goal_value_paths": ["npi", "locations"]}],
                "terminal_verifier_expectations": [{"goal_value_paths": ["npi", "locations"]}],
            }
        }
        ctx.workflow_verification_evidence.code_artifact_metadata = dict(ctx.code_artifact_metadata)
        ctx.latest_recorded_build_test_outcome = RecordedBuildTestOutcome(
            phase="persisted_block_run",
            attempted_tool="update_and_run_blocks",
            verdict="repairable_failure",
            reason_code="outcome_not_demonstrated",
            structural_failure_identity="completion:unsatisfied-output",
            authored_structure_signature="authored:previous-failed-candidate",
        )
        candidate_yaml = _yaml(
            f"""
            title: Provider lookup
            workflow_definition:
              blocks:
              - block_type: code
                label: {label}
                code: |
                  await page.locator("#locInput").wait_for(state="visible", timeout=15000)
                  npi = "1234567890"
                  locations = [{{"address": "123 Main St"}}]
            """
        )

        result = await _update_workflow({"workflow_yaml": candidate_yaml}, ctx, allow_missing_credentials=True)

        assert result["ok"] is True
        labels = [block.get("label") for block in workflow_blocks(parse_workflow_yaml(ctx.workflow_yaml))]
        assert labels == [label]

    @pytest.mark.asyncio
    async def test_authoritative_outcome_not_demonstrated_accepts_canonical_output_path_candidate_with_sibling(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        label = "submit_form_and_extract_confirmation"
        schema = workflow_update_module._schema_template_text_for_required_paths({"output.confirmation_number"})
        ctx.code_artifact_metadata = {
            label: {
                "block_label": label,
                "claimed_outcomes": [{"goal_value_paths": ["output.confirmation_number"], "extraction_schema": schema}],
                "terminal_verifier_expectations": [
                    {"goal_value_paths": ["output.confirmation_number"], "extraction_schema": schema}
                ],
            }
        }
        ctx.workflow_verification_evidence.code_artifact_metadata = dict(ctx.code_artifact_metadata)
        ctx.latest_recorded_build_test_outcome = RecordedBuildTestOutcome(
            phase="persisted_block_run",
            attempted_tool="update_and_run_blocks",
            verdict="repairable_failure",
            reason_code="outcome_not_demonstrated",
            structural_failure_identity="completion:unsatisfied-output",
            authored_structure_signature="authored:previous-failed-candidate",
            missing_requested_output_facts=[
                {
                    "output_path": "output.confirmation_number",
                    "output_root": "output",
                    "value_status": "no_typed_value",
                },
            ],
        )
        candidate_yaml = _yaml(
            f"""
            title: Form submission
            workflow_definition:
              blocks:
              - block_type: code
                label: {label}
                code: |
                  return {{"output": {{"confirmation_number": "ABC-123", "account_number": "100245"}}}}
            """
        )

        result = await _update_workflow({"workflow_yaml": candidate_yaml}, ctx, allow_missing_credentials=True)

        assert result["ok"] is True
        assert ctx.workflow_yaml is not None
        assert "confirmation_number" in ctx.workflow_yaml
        assert ctx.code_artifact_metadata[label]["claimed_outcomes"][0]["extraction_schema"] == schema
        assert ctx.latest_recorded_build_test_outcome is not None
        assert ctx.latest_recorded_build_test_outcome.reason_code == "outcome_not_demonstrated"

    @pytest.mark.asyncio
    async def test_authoritative_outcome_not_demonstrated_allows_unprovable_dynamic_output_shape(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        label = "lookup_provider_and_extract_credentials"
        ctx.code_artifact_metadata = {
            label: {
                "block_label": label,
                "claimed_outcomes": [{"goal_value_paths": ["npi"]}],
                "terminal_verifier_expectations": [{"goal_value_paths": ["npi"]}],
            }
        }
        ctx.workflow_verification_evidence.code_artifact_metadata = dict(ctx.code_artifact_metadata)
        ctx.latest_recorded_build_test_outcome = RecordedBuildTestOutcome(
            phase="persisted_block_run",
            attempted_tool="update_and_run_blocks",
            verdict="repairable_failure",
            reason_code="outcome_not_demonstrated",
            structural_failure_identity="completion:unsatisfied-output",
            authored_structure_signature="authored:previous-failed-candidate",
            missing_requested_output_facts=[
                {"output_path": "npi", "output_root": "npi", "value_status": "no_typed_value"},
            ],
        )
        candidate_yaml = _yaml(
            f"""
            title: Provider lookup
            workflow_definition:
              blocks:
              - block_type: code
                label: {label}
                code: |
                  result = {{}}
                  key = "npi"
                  value = await page.locator("#npi").inner_text(timeout=5000)
                  result[key] = value
                  return result
            """
        )

        result = await _update_workflow({"workflow_yaml": candidate_yaml}, ctx, allow_missing_credentials=True)

        assert result["ok"] is True
        labels = [block.get("label") for block in workflow_blocks(parse_workflow_yaml(ctx.workflow_yaml))]
        assert labels == [label]
        outcome = ctx.latest_recorded_build_test_outcome
        assert outcome is not None
        assert outcome.reason_code == "outcome_not_demonstrated"
        assert all(
            fact.get("reason_code") != "recorded_outcome_missing_output_coverage"
            for fact in outcome.missing_requested_output_facts
        )

    @pytest.mark.asyncio
    async def test_authoritative_outcome_not_demonstrated_allows_literal_list_of_output_dicts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        label = "lookup_provider_and_extract_credentials"
        ctx.code_artifact_metadata = {
            label: {
                "block_label": label,
                "claimed_outcomes": [{"goal_value_paths": ["npi"]}],
                "terminal_verifier_expectations": [{"goal_value_paths": ["npi"]}],
            }
        }
        ctx.workflow_verification_evidence.code_artifact_metadata = dict(ctx.code_artifact_metadata)
        ctx.latest_recorded_build_test_outcome = RecordedBuildTestOutcome(
            phase="persisted_block_run",
            attempted_tool="update_and_run_blocks",
            verdict="repairable_failure",
            reason_code="outcome_not_demonstrated",
            structural_failure_identity="completion:unsatisfied-output",
            authored_structure_signature="authored:previous-failed-candidate",
            missing_requested_output_facts=[
                {"output_path": "npi", "output_root": "npi", "value_status": "no_typed_value"},
            ],
        )
        candidate_yaml = _yaml(
            f"""
            title: Provider lookup
            workflow_definition:
              blocks:
              - block_type: code
                label: {label}
                code: |
                  return [{{"npi": "123"}}]
            """
        )

        result = await _update_workflow({"workflow_yaml": candidate_yaml}, ctx, allow_missing_credentials=True)

        assert result["ok"] is True
        assert ctx.workflow_yaml == candidate_yaml
        outcome = ctx.latest_recorded_build_test_outcome
        assert outcome is not None
        assert outcome.reason_code == "outcome_not_demonstrated"
        assert all(
            fact.get("reason_code") != "recorded_outcome_missing_output_coverage"
            for fact in outcome.missing_requested_output_facts
        )

    @pytest.mark.asyncio
    async def test_authoritative_outcome_not_demonstrated_allows_dynamic_list_output_to_abstain(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        label = "lookup_provider_and_extract_credentials"
        ctx.code_artifact_metadata = {
            label: {
                "block_label": label,
                "claimed_outcomes": [{"goal_value_paths": ["npi"]}],
                "terminal_verifier_expectations": [{"goal_value_paths": ["npi"]}],
            }
        }
        ctx.workflow_verification_evidence.code_artifact_metadata = dict(ctx.code_artifact_metadata)
        ctx.latest_recorded_build_test_outcome = RecordedBuildTestOutcome(
            phase="persisted_block_run",
            attempted_tool="update_and_run_blocks",
            verdict="repairable_failure",
            reason_code="outcome_not_demonstrated",
            structural_failure_identity="completion:unsatisfied-output",
            authored_structure_signature="authored:previous-failed-candidate",
            missing_requested_output_facts=[
                {"output_path": "npi", "output_root": "npi", "value_status": "no_typed_value"},
            ],
        )
        candidate_yaml = _yaml(
            f"""
            title: Provider lookup
            workflow_definition:
              blocks:
              - block_type: code
                label: {label}
                code: |
                  record = {{"npi": "123"}}
                  return [record]
            """
        )

        result = await _update_workflow({"workflow_yaml": candidate_yaml}, ctx, allow_missing_credentials=True)

        assert result["ok"] is True
        assert ctx.workflow_yaml == candidate_yaml
        outcome = ctx.latest_recorded_build_test_outcome
        assert outcome is not None
        assert outcome.reason_code == "outcome_not_demonstrated"
        assert all(
            fact.get("reason_code") != "recorded_outcome_missing_output_coverage"
            for fact in outcome.missing_requested_output_facts
        )

    @pytest.mark.asyncio
    async def test_authoritative_outcome_not_demonstrated_allows_dynamic_key_list_dict_to_abstain(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        label = "lookup_provider_and_extract_credentials"
        ctx.code_artifact_metadata = {
            label: {
                "block_label": label,
                "claimed_outcomes": [{"goal_value_paths": ["npi"]}],
                "terminal_verifier_expectations": [{"goal_value_paths": ["npi"]}],
            }
        }
        ctx.workflow_verification_evidence.code_artifact_metadata = dict(ctx.code_artifact_metadata)
        ctx.latest_recorded_build_test_outcome = RecordedBuildTestOutcome(
            phase="persisted_block_run",
            attempted_tool="update_and_run_blocks",
            verdict="repairable_failure",
            reason_code="outcome_not_demonstrated",
            structural_failure_identity="completion:unsatisfied-output",
            authored_structure_signature="authored:previous-failed-candidate",
            missing_requested_output_facts=[
                {"output_path": "npi", "output_root": "npi", "value_status": "no_typed_value"},
            ],
        )
        candidate_yaml = _yaml(
            f"""
            title: Provider lookup
            workflow_definition:
              blocks:
              - block_type: code
                label: {label}
                code: |
                  key = "npi"
                  return [{{key: "123"}}]
            """
        )

        result = await _update_workflow({"workflow_yaml": candidate_yaml}, ctx, allow_missing_credentials=True)

        assert result["ok"] is True
        assert ctx.workflow_yaml == candidate_yaml
        outcome = ctx.latest_recorded_build_test_outcome
        assert outcome is not None
        assert outcome.reason_code == "outcome_not_demonstrated"
        assert all(
            fact.get("reason_code") != "recorded_outcome_missing_output_coverage"
            for fact in outcome.missing_requested_output_facts
        )

    @pytest.mark.asyncio
    async def test_authoritative_outcome_not_demonstrated_allows_dynamic_key_local_dict_to_abstain(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        label = "lookup_provider_and_extract_credentials"
        ctx.code_artifact_metadata = {
            label: {
                "block_label": label,
                "claimed_outcomes": [{"goal_value_paths": ["npi"]}],
                "terminal_verifier_expectations": [{"goal_value_paths": ["npi"]}],
            }
        }
        ctx.workflow_verification_evidence.code_artifact_metadata = dict(ctx.code_artifact_metadata)
        ctx.latest_recorded_build_test_outcome = RecordedBuildTestOutcome(
            phase="persisted_block_run",
            attempted_tool="update_and_run_blocks",
            verdict="repairable_failure",
            reason_code="outcome_not_demonstrated",
            structural_failure_identity="completion:unsatisfied-output",
            authored_structure_signature="authored:previous-failed-candidate",
            missing_requested_output_facts=[
                {"output_path": "npi", "output_root": "npi", "value_status": "no_typed_value"},
            ],
        )
        candidate_yaml = _yaml(
            f"""
            title: Provider lookup
            workflow_definition:
              blocks:
              - block_type: code
                label: {label}
                code: |
                  key = "npi"
                  result = {{key: "123"}}
                  return result
            """
        )

        result = await _update_workflow({"workflow_yaml": candidate_yaml}, ctx, allow_missing_credentials=True)

        assert result["ok"] is True
        assert ctx.workflow_yaml == candidate_yaml
        outcome = ctx.latest_recorded_build_test_outcome
        assert outcome is not None
        assert outcome.reason_code == "outcome_not_demonstrated"
        assert all(
            fact.get("reason_code") != "recorded_outcome_missing_output_coverage"
            for fact in outcome.missing_requested_output_facts
        )

    @pytest.mark.asyncio
    async def test_authoritative_outcome_not_demonstrated_allows_helper_dynamic_key_local_dict_to_abstain(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        label = "lookup_provider_and_extract_credentials"
        ctx.code_artifact_metadata = {
            label: {
                "block_label": label,
                "claimed_outcomes": [{"goal_value_paths": ["npi"]}],
                "terminal_verifier_expectations": [{"goal_value_paths": ["npi"]}],
            }
        }
        ctx.workflow_verification_evidence.code_artifact_metadata = dict(ctx.code_artifact_metadata)
        ctx.latest_recorded_build_test_outcome = RecordedBuildTestOutcome(
            phase="persisted_block_run",
            attempted_tool="update_and_run_blocks",
            verdict="repairable_failure",
            reason_code="outcome_not_demonstrated",
            structural_failure_identity="completion:unsatisfied-output",
            authored_structure_signature="authored:previous-failed-candidate",
            missing_requested_output_facts=[
                {"output_path": "npi", "output_root": "npi", "value_status": "no_typed_value"},
            ],
        )
        candidate_yaml = _yaml(
            f"""
            title: Provider lookup
            workflow_definition:
              blocks:
              - block_type: code
                label: {label}
                code: |
                  def build():
                      key = "npi"
                      result = {{key: "123"}}
                      return result

                  return build()
            """
        )

        result = await _update_workflow({"workflow_yaml": candidate_yaml}, ctx, allow_missing_credentials=True)

        assert result["ok"] is True
        assert ctx.workflow_yaml == candidate_yaml
        outcome = ctx.latest_recorded_build_test_outcome
        assert outcome is not None
        assert outcome.reason_code == "outcome_not_demonstrated"
        assert all(
            fact.get("reason_code") != "recorded_outcome_missing_output_coverage"
            for fact in outcome.missing_requested_output_facts
        )

    @pytest.mark.asyncio
    async def test_authoritative_outcome_not_demonstrated_allows_exact_missing_array_child_paths(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        label = "lookup_entry"
        child_paths = ["output.npi", "output.locations[].address", "output.locations[].status", "output.statuses"]
        schema = (
            '{"type":"object","properties":{"output":{"type":"object","properties":{'
            '"npi":{"type":"string"},'
            '"locations":{"type":"array","items":{"type":"object","properties":{'
            '"address":{"type":"string"},"status":{"type":"string"}}}},'
            '"statuses":{"type":"array","items":{"type":"string"}}}}}}'
        )
        ctx.code_artifact_metadata = {
            label: {
                "block_label": label,
                "claimed_outcomes": [{"goal_value_paths": child_paths, "extraction_schema": schema}],
                "terminal_verifier_expectations": [{"goal_value_paths": child_paths, "extraction_schema": schema}],
            }
        }
        ctx.workflow_verification_evidence.code_artifact_metadata = dict(ctx.code_artifact_metadata)
        ctx.latest_recorded_build_test_outcome = RecordedBuildTestOutcome(
            phase="persisted_block_run",
            attempted_tool="update_and_run_blocks",
            verdict="repairable_failure",
            reason_code="outcome_not_demonstrated",
            structural_failure_identity="completion:unsatisfied-output",
            authored_structure_signature="authored:previous-failed-candidate",
            missing_requested_output_facts=[
                {"output_path": "output.npi", "output_root": "output", "value_status": "no_typed_value"},
                {
                    "output_path": "output.locations[].address",
                    "output_root": "output",
                    "value_status": "no_typed_value",
                },
                {
                    "output_path": "output.locations[].status",
                    "output_root": "output",
                    "value_status": "no_typed_value",
                },
                {"output_path": "output.statuses", "output_root": "output", "value_status": "no_typed_value"},
            ],
        )
        candidate_yaml = _yaml(
            f"""
            title: Entry lookup
            workflow_definition:
              blocks:
              - block_type: code
                label: {label}
                code: |
                  return {{
                      "output": {{
                          "npi": "1234567890",
                          "locations": [{{"address": "Example location", "status": "active"}}],
                          "statuses": ["active"],
                      }}
                  }}
            """
        )

        result = await _update_workflow({"workflow_yaml": candidate_yaml}, ctx, allow_missing_credentials=True)

        assert result["ok"] is True
        labels = [block.get("label") for block in workflow_blocks(parse_workflow_yaml(ctx.workflow_yaml))]
        assert labels == [label]

    def test_single_output_contract_evaluator_reports_all_deficiency_classes(self) -> None:
        ctx = _code_only_ctx()
        ctx.scout_trajectory = [
            {"tool_name": "type_text", "selector": "#filter", "source_url": "https://example.com/records"},
            {"tool_name": "click", "selector": "#choose", "source_url": "https://example.com/records"},
        ]
        ctx.request_policy = RequestPolicy(
            completion_criteria=[
                _typed_completion_criterion(
                    id="requested_value",
                    output_path="output.record_id",
                    level="run",
                    method_mandated=False,
                    kind="outcome",
                )
            ]
        )
        workflow_yaml = _yaml(
            """
            title: Entry lookup
            workflow_definition:
              parameters:
              - parameter_type: workflow
                workflow_parameter_type: string
                key: value
              blocks:
              - block_type: code
                label: extract_record
                parameter_keys:
                - value
                code: |
                  _scout_entry_target = page.locator("#filter")
                  try:
                      await _scout_entry_target.wait_for(state="visible", timeout=1000)
                  except Exception:
                      await page.goto("https://example.com/records", wait_until="domcontentloaded")
                      await _scout_entry_target.wait_for(state="visible")
                  await page.locator("#filter").fill(str(value))
                  await page.locator("#choose").click()
                  await page.wait_for_load_state("domcontentloaded")
                  return {"output": {"summary": "found"}}
            """
        )

        evaluation = workflow_update_module._evaluate_output_contract_for_code_block(ctx, workflow_yaml, [])

        assert evaluation is not None
        assert evaluation.has_deficiencies is True
        assert evaluation.block_label == "extract_record"
        assert evaluation.missing_metadata_paths == ["output.record_id"]
        assert evaluation.missing_schema_paths == ["output.record_id"]
        assert evaluation.missing_return_paths == ["output.record_id"]
        assert evaluation.shape_violations == ["separated_spine_shape_required"]
        assert evaluation.payload["satisfying_templates"]["code_artifact_metadata"]["block_label"] == "extract_record"
        assert evaluation.payload["satisfying_templates"]["return_skeleton"] == (
            'return {"output": {"record_id": record_id}}'
        )

    def test_schema_template_derives_nested_array_paths_without_semantic_values(self) -> None:
        schema = workflow_update_module._schema_template_for_required_paths(
            {"output.npi", "output.locations[].address", "output.locations[].status"}
        )

        output = schema["properties"]["output"]
        assert output["type"] == "object"
        assert output["required"] == ["locations", "npi"]
        locations = output["properties"]["locations"]
        assert locations["type"] == "array"
        assert locations["items"]["required"] == ["address", "status"]
        assert locations["items"]["properties"]["address"] == {}
        assert locations["items"]["properties"]["status"] == {}
        assert "description" not in json.dumps(schema)
        assert "example" not in json.dumps(schema)

    def test_static_return_uncertainty_requires_typed_advisory_grant(self) -> None:
        ctx = _code_only_ctx()
        ctx.last_code_authoring_repair_context = CodeAuthoringRepairContext(
            block_label="extract_entry_output",
            reason_code="metadata_reject",
            required_goal_value_paths=["output.record_id"],
            required_extraction_schema_paths=["output.record_id"],
            required_code_return_paths=["output.record_id"],
            metadata_contract_source="requested_output_contract",
            metadata_contract_reason_code="requested_output_contract_missing_output_coverage",
        )
        schema = workflow_update_module._schema_template_text_for_required_paths({"output.record_id"})
        metadata = [
            {
                "block_label": "extract_entry_output",
                "claimed_outcomes": [{"goal_value_paths": ["output.record_id"], "extraction_schema": schema}],
                "terminal_verifier_expectations": [
                    {"goal_value_paths": ["output.record_id"], "extraction_schema": schema}
                ],
            }
        ]
        workflow_yaml = _yaml(
            """
            title: Entry lookup
            workflow_definition:
              blocks:
              - block_type: code
                label: extract_entry_output
                code: |
                  return {"output": build_output()}
            """
        )

        update_eval = workflow_update_module._evaluate_output_contract_for_code_block(
            ctx, workflow_yaml, metadata, allow_static_return_advisory=False
        )
        run_eval = workflow_update_module._evaluate_output_contract_for_code_block(
            ctx, workflow_yaml, metadata, allow_static_return_advisory=True
        )

        assert update_eval is not None
        assert update_eval.missing_return_paths == ["output.record_id"]
        assert update_eval.can_attempt_run is False
        assert run_eval is not None
        assert run_eval.missing_return_paths == ["output.record_id"]
        assert run_eval.can_attempt_run is False

        workflow_update_module._grant_output_contract_advisory_run(ctx, run_eval.canonical_signature)
        granted_eval = workflow_update_module._evaluate_output_contract_for_code_block(
            ctx, workflow_yaml, metadata, allow_static_return_advisory=True
        )

        assert granted_eval is not None
        assert granted_eval.missing_return_paths == []
        assert granted_eval.payload["static_return_advisory_paths"] == ["output.record_id"]
        assert granted_eval.payload["actuated_static_return_advisory"] is True
        assert granted_eval.can_attempt_run is True

    @pytest.mark.asyncio
    async def test_run_path_allows_typed_advisory_declared_output_return_shape_gap(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        ctx.last_code_authoring_repair_context = CodeAuthoringRepairContext(
            block_label="extract_entry_output",
            reason_code="metadata_reject",
            required_goal_value_paths=["output.record_id"],
            required_extraction_schema_paths=["output.record_id"],
            required_code_return_paths=["output.record_id"],
            metadata_contract_source="requested_output_contract",
            metadata_contract_reason_code="requested_output_contract_missing_output_coverage",
        )
        workflow_yaml = _yaml(
            """
            title: Entry lookup
            workflow_definition:
              blocks:
              - block_type: code
                label: extract_entry_output
                code: |
                  return "not a structured output"
            """
        )
        ctx.turn_id = "run-static-return-gap"
        signature = workflow_update_module._output_contract_signature(ctx=ctx, required_paths={"output.record_id"})
        workflow_update_module._grant_output_contract_advisory_run(ctx, signature)

        result = await _update_workflow(
            {"workflow_yaml": workflow_yaml},
            ctx,
            allow_missing_credentials=True,
            allow_static_output_uncertainty=True,
        )

        assert result["ok"] is True
        assert ctx.code_artifact_metadata["extract_entry_output"]["claimed_outcomes"][0]["goal_value_paths"] == [
            "output.record_id"
        ]
        assert ctx.code_artifact_metadata["extract_entry_output"]["claimed_outcomes"][0]["extraction_schema"]
        parsed = parse_workflow_yaml(ctx.workflow_yaml)
        block = _single_code_block(parsed)
        schema = json.loads(block["extraction_schema"])
        assert schema["properties"]["output"]["properties"]["record_id"] == {}

    @pytest.mark.asyncio
    async def test_run_path_persists_effective_output_contract_for_readback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _code_only_ctx()
        required_paths = {"output.record_id"}
        ctx.last_code_authoring_repair_context = CodeAuthoringRepairContext(
            block_label="extract_entry_output",
            reason_code="metadata_reject",
            required_goal_value_paths=["output.record_id"],
            required_extraction_schema_paths=["output.record_id"],
            required_code_return_paths=["output.record_id"],
            metadata_contract_source="requested_output_contract",
            metadata_contract_reason_code="requested_output_contract_missing_output_coverage",
        )
        schema = workflow_update_module._schema_template_text_for_required_paths(required_paths)
        metadata = [
            {
                "block_label": "extract_entry_output",
                "claimed_outcomes": [{"goal_value_paths": ["output.record_id"], "extraction_schema": schema}],
                "terminal_verifier_expectations": [
                    {"goal_value_paths": ["output.record_id"], "extraction_schema": schema}
                ],
            }
        ]
        workflow_yaml = _yaml(
            """
            title: Entry lookup
            workflow_definition:
              blocks:
              - block_type: code
                label: extract_entry_output
                code: |
                  return {"output": {"record_id": "ABC123"}}
            """
        )

        def fake_workflow(*, blocks: list[object]) -> SimpleNamespace:
            return SimpleNamespace(
                title="Entry lookup",
                description=None,
                workflow_definition=SimpleNamespace(blocks=blocks, parameters=[]),
                proxy_location=None,
                webhook_callback_url=None,
                totp_verification_url=None,
                totp_identifier=None,
                persist_browser_session=False,
                pin_saved_session_ip=False,
                browser_profile_id=None,
                browser_profile_key=None,
                model=None,
                max_screenshot_scrolls=None,
                extra_http_headers=None,
                cdp_connect_headers=None,
                run_with=None,
                ai_fallback=None,
                cache_key=None,
                adaptive_caching=None,
                enable_self_healing=None,
                code_version=None,
                run_sequentially=False,
                sequential_key=None,
            )

        persisted: dict[str, object] = {}

        async def fake_get_prior_workflow(_ctx: CopilotContext) -> SimpleNamespace:
            return fake_workflow(blocks=[])

        async def fake_process_workflow_yaml(**_kwargs: object) -> SimpleNamespace:
            return fake_workflow(blocks=[SimpleNamespace(label="extract_entry_output")])

        async def fake_update_workflow_definition(**kwargs: object) -> None:
            persisted.update(kwargs)

        monkeypatch.setattr(workflow_update_module, "_get_prior_workflow", fake_get_prior_workflow)
        monkeypatch.setattr(workflow_update_module, "_process_workflow_yaml", fake_process_workflow_yaml)
        monkeypatch.setattr(
            workflow_update_module.app.WORKFLOW_SERVICE,
            "update_workflow_definition",
            fake_update_workflow_definition,
        )
        monkeypatch.setattr(
            workflow_update_module,
            "resolve_copilot_created_by_stamp",
            AsyncMock(return_value="copilot"),
        )

        result = await _update_workflow(
            {"workflow_yaml": workflow_yaml, "code_artifact_metadata": metadata},
            ctx,
            allow_missing_credentials=True,
            allow_static_output_uncertainty=True,
        )

        assert result["ok"] is True
        assert persisted["workflow_id"] == "w"
        definition = persisted["workflow_definition"]
        assert definition.blocks == [SimpleNamespace(label="extract_entry_output")]
        parsed = parse_workflow_yaml(ctx.workflow_yaml)
        block = _single_code_block(parsed)
        assert json.loads(block["extraction_schema"])["properties"]["output"]["properties"]["record_id"] == {}

    @pytest.mark.asyncio
    async def test_run_path_persists_blocks_for_api_readback_when_metadata_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _code_only_ctx()
        workflow_yaml = _yaml(
            """
            title: Public path validation
            workflow_definition:
              blocks:
              - block_type: code
                label: validate_public_path
                code: |
                  return {"public_form_exists": False}
            """
        )

        def fake_workflow(*, blocks: list[object]) -> SimpleNamespace:
            return SimpleNamespace(
                title="Public path validation",
                description=None,
                workflow_definition=SimpleNamespace(blocks=blocks, parameters=[]),
                proxy_location=None,
                webhook_callback_url=None,
                totp_verification_url=None,
                totp_identifier=None,
                persist_browser_session=False,
                pin_saved_session_ip=False,
                browser_profile_id=None,
                browser_profile_key=None,
                model=None,
                max_screenshot_scrolls=None,
                extra_http_headers=None,
                cdp_connect_headers=None,
                run_with=None,
                ai_fallback=None,
                cache_key=None,
                adaptive_caching=None,
                enable_self_healing=None,
                code_version=None,
                run_sequentially=False,
                sequential_key=None,
            )

        persisted: dict[str, object] = {}

        async def fake_get_prior_workflow(_ctx: CopilotContext) -> SimpleNamespace:
            return fake_workflow(blocks=[])

        async def fake_process_workflow_yaml(**_kwargs: object) -> SimpleNamespace:
            return fake_workflow(blocks=[SimpleNamespace(label="validate_public_path")])

        async def fake_update_workflow_definition(**kwargs: object) -> None:
            persisted.update(kwargs)

        monkeypatch.setattr(workflow_update_module, "_get_prior_workflow", fake_get_prior_workflow)
        monkeypatch.setattr(workflow_update_module, "_process_workflow_yaml", fake_process_workflow_yaml)
        monkeypatch.setattr(
            workflow_update_module.app.WORKFLOW_SERVICE,
            "update_workflow_definition",
            fake_update_workflow_definition,
        )
        monkeypatch.setattr(
            workflow_update_module,
            "resolve_copilot_created_by_stamp",
            AsyncMock(return_value="copilot"),
        )

        result = await _update_workflow(
            {"workflow_yaml": workflow_yaml},
            ctx,
            allow_missing_credentials=True,
            allow_static_output_uncertainty=True,
        )

        assert result["ok"] is True
        assert persisted["workflow_id"] == "w"
        assert persisted["workflow_definition"].blocks == [SimpleNamespace(label="validate_public_path")]

    def test_output_contract_signature_uses_stable_scope_and_required_path_identity(self) -> None:
        ctx = _code_only_ctx()
        ctx.turn_id = "contract-scope-a"
        first = workflow_update_module._output_contract_signature(
            ctx=ctx,
            required_paths={"output.locations[].address", "output.statuses"},
        )
        second = workflow_update_module._output_contract_signature(
            ctx=ctx,
            required_paths={"output.statuses", "output.locations[].address"},
        )
        other_ctx = _code_only_ctx()
        other_ctx.turn_id = "contract-scope-b"
        other_scope = workflow_update_module._output_contract_signature(
            ctx=other_ctx,
            required_paths={"output.statuses", "output.locations[].address"},
        )

        assert first == second
        assert other_scope != first
        assert (
            workflow_update_module._output_contract_pin_key(
                ctx,
                "title: First\nworkflow_definition:\n  blocks: []\n",
                {"output.locations[].address", "output.statuses"},
            )
            == first
        )

    def test_output_contract_owner_pin_is_scoped(self) -> None:
        ctx = _code_only_ctx()
        workflow_yaml = _yaml(
            """
            title: Shared output shape
            workflow_definition:
              blocks:
              - block_type: code
                label: first_goal_output
                code: |
                  return {"output": {"status": "one"}}
              - block_type: code
                label: second_goal_output
                code: |
                  return {"output": {"status": "two"}}
            """
        )
        required_paths = {"output.status"}

        ctx.turn_id = "first-build-goal"
        workflow_update_module._pin_output_contract_block_label(
            ctx,
            workflow_yaml,
            required_paths,
            "first_goal_output",
        )
        ctx.turn_id = "second-build-goal"
        workflow_update_module._pin_output_contract_block_label(
            ctx,
            workflow_yaml,
            required_paths,
            "second_goal_output",
        )

        assert workflow_update_module._pinned_output_contract_block_label(ctx, workflow_yaml, required_paths) == (
            "second_goal_output"
        )
        ctx.turn_id = "first-build-goal"
        assert workflow_update_module._pinned_output_contract_block_label(ctx, workflow_yaml, required_paths) == (
            "first_goal_output"
        )

    @pytest.mark.asyncio
    async def test_output_contract_first_contact_imposes_keyed_return_envelope(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        ctx.request_policy = RequestPolicy(
            completion_criteria=[
                _typed_completion_criterion(
                    id="requested_value",
                    output_path="output.record_id",
                    level="run",
                    method_mandated=False,
                    kind="outcome",
                ),
                _typed_completion_criterion(
                    id="requested_flags",
                    output_path="output.flags",
                    level="run",
                    method_mandated=False,
                    kind="outcome",
                ),
            ]
        )
        workflow_yaml = _yaml(
            """
            title: Entry lookup
            workflow_definition:
              blocks:
              - block_type: code
                label: extract_entry_output
                code: |
                  record_id = "ABC123"
                  flags = ["enabled"]
            """
        )

        result = await _update_workflow({"workflow_yaml": workflow_yaml}, ctx, allow_missing_credentials=True)

        assert result["ok"] is True, result
        parsed = parse_workflow_yaml(ctx.workflow_yaml)
        assert isinstance(parsed, dict)
        code = str(_single_code_block(parsed)["code"])
        assert 'return {"output": {"flags": flags, "record_id": record_id}}' in code
        metadata = ctx.code_artifact_metadata["extract_entry_output"]
        assert metadata["claimed_outcomes"][0]["goal_value_paths"] == ["output.flags", "output.record_id"]
        assert (
            json.loads(metadata["claimed_outcomes"][0]["extraction_schema"])["properties"]["output"]["properties"][
                "record_id"
            ]
            == {}
        )

    def _run_preflight_metadata_reject_ctx(self) -> CopilotContext:
        ctx = _code_only_ctx()
        ctx.last_code_authoring_repair_context = CodeAuthoringRepairContext(
            block_label="validate_start",
            reason_code="metadata_reject",
            required_goal_value_paths=["output.record_id"],
            required_extraction_schema_paths=["output.record_id"],
            required_code_return_paths=["output.record_id"],
            metadata_contract_source="requested_output_contract",
            metadata_contract_reason_code="requested_output_contract_missing_output_coverage",
        )
        return ctx

    _RUN_PREFLIGHT_CLICK_YAML = _yaml(
        """
        title: Provider lookup
        workflow_definition:
          blocks:
          - block_type: code
            label: validate_start
            code: |
              await page.locator("#start").click()
        """
    )

    def test_metadata_contract_scaffold_uses_recorded_paths_before_request_policy(self) -> None:
        ctx = _code_only_ctx()
        ctx.request_policy = RequestPolicy(
            completion_criteria=[
                _typed_completion_criterion(
                    id="requested_value",
                    output_path="output.requested_value",
                    level="run",
                    method_mandated=False,
                    kind="outcome",
                )
            ]
        )
        ctx.latest_recorded_build_test_outcome = RecordedBuildTestOutcome(
            phase="persisted_block_run",
            attempted_tool="update_and_run_blocks",
            verdict="repairable_failure",
            reason_code="outcome_not_demonstrated",
            structural_failure_identity="completion:typed-output",
            missing_requested_output_facts=[
                {"output_path": "output.recorded_value", "output_root": "output", "value_status": "no_typed_value"},
            ],
        )
        workflow_yaml = _yaml(
            """
            title: Entry lookup
            workflow_definition:
              blocks:
              - block_type: code
                label: extract_entry_output
                code: |
                  return {"output": {"recorded_value": "ABC123"}}
            """
        )
        required_paths, source, reason_code = workflow_update_module._required_child_output_paths_for_authoring(ctx)

        scaffolded = workflow_update_module._apply_metadata_contract_scaffold(
            ctx,
            workflow_yaml,
            [],
            required_paths=required_paths,
            source=source,
            reason_code=reason_code,
        )

        assert scaffolded[0]["artifact_id"] == "code_artifact:extract_entry_output"
        assert scaffolded[0]["block_label"] == "extract_entry_output"
        assert scaffolded[0]["claimed_outcomes"][0]["goal_value_paths"] == ["output.recorded_value"]
        schema = json.loads(scaffolded[0]["claimed_outcomes"][0]["extraction_schema"])
        assert schema["properties"]["output"]["properties"]["recorded_value"] == {}

    def test_independent_judgment_output_is_not_required_as_code_return_path(self) -> None:
        judgment_criterion = CompletionCriterion(
            id="login_gate",
            outcome="the target path is blocked by a login gate",
            output_path="output.login_gate_blocks_target",
            expected_output_shape="goal_judgment_boolean",
            requested_output_evidence_source="independent_run_evidence",
            judgment_truth_condition=JudgmentTruthCondition(
                predicate="login_gate_blocks_target",
                polarity_when_holds=True,
            ),
        )
        judgment_only_ctx = _code_only_ctx()
        judgment_only_ctx.request_policy = RequestPolicy(completion_criteria=[judgment_criterion])

        judgment_only_paths = workflow_update_module._output_contract_required_paths_source(judgment_only_ctx).union

        assert judgment_only_paths == set()

        ctx = _code_only_ctx()
        ctx.request_policy = RequestPolicy(
            completion_criteria=[
                judgment_criterion,
                CompletionCriterion(
                    id="record_id",
                    outcome="the record id is returned",
                    output_path="output.record_id",
                ),
            ]
        )

        contract = workflow_update_module._output_contract_required_paths_source(ctx)
        required_paths, source, reason_code = contract.union, contract.source, contract.reason_code

        assert required_paths == {"output.record_id"}
        assert source == "requested_output_contract"
        assert reason_code == "requested_output_contract_missing_output_coverage"

    def test_independent_judgment_shape_output_is_not_required_as_code_return_path(self) -> None:
        ctx = _code_only_ctx()
        ctx.request_policy = RequestPolicy(
            completion_criteria=[
                CompletionCriterion(
                    id="login_gate",
                    outcome="the target path is blocked by a login gate",
                    output_path="output.login_gate_blocks_target",
                    expected_output_value=True,
                    expected_output_shape="goal_judgment_boolean",
                    requested_output_evidence_source="independent_run_evidence",
                )
            ]
        )

        required_paths = workflow_update_module._output_contract_required_paths_source(ctx).union

        assert required_paths == set()

    def test_mixed_repair_context_keeps_non_judgment_code_return_path(self) -> None:
        ctx = _code_only_ctx()
        ctx.request_policy = RequestPolicy(
            completion_criteria=[
                CompletionCriterion(
                    id="login_gate",
                    outcome="the target path is blocked by a login gate",
                    output_path="output.login_gate_blocks_target",
                    expected_output_shape="goal_judgment_boolean",
                    requested_output_evidence_source="independent_run_evidence",
                    judgment_truth_condition=JudgmentTruthCondition(
                        predicate="login_gate_blocks_target",
                        polarity_when_holds=True,
                    ),
                )
            ]
        )
        ctx.last_code_authoring_repair_context = CodeAuthoringRepairContext(
            block_label="extract_entry_output",
            reason_code="metadata_reject",
            required_goal_value_paths=["output.login_gate_blocks_target", "output.record_id"],
            required_extraction_schema_paths=["output.login_gate_blocks_target", "output.record_id"],
            required_code_return_paths=["output.login_gate_blocks_target", "output.record_id"],
            metadata_contract_source="requested_output_contract",
            metadata_contract_reason_code="requested_output_contract_missing_output_coverage",
        )

        contract = workflow_update_module._output_contract_required_paths_source(ctx)
        required_paths, source, reason_code = contract.union, contract.source, contract.reason_code

        assert required_paths == {"output.record_id"}
        assert source == "requested_output_contract"
        assert reason_code == "requested_output_contract_missing_output_coverage"

    def test_runtime_output_judgment_is_excluded_from_scalar_bind_set(self) -> None:
        ctx = _code_only_ctx()
        ctx.request_policy = RequestPolicy(
            completion_criteria=[
                CompletionCriterion(
                    id="login_gate",
                    outcome="the target path is blocked by a login gate",
                    output_path="output.login_gate_blocks_target",
                    expected_output_value=True,
                    expected_output_shape="goal_judgment_boolean",
                    requested_output_evidence_source="runtime_output",
                ),
                CompletionCriterion(
                    id="record_id",
                    outcome="the record id is returned",
                    output_path="output.record_id",
                ),
            ]
        )

        contract = workflow_update_module._output_contract_required_paths_source(ctx)

        assert contract.observation_paths == {"output.record_id"}
        assert contract.union == {"output.record_id"}
        assert "output.login_gate_blocks_target" not in contract.declaration_paths

    def test_runtime_output_judgment_alone_yields_empty_bind_set(self) -> None:
        ctx = _code_only_ctx()
        ctx.request_policy = RequestPolicy(
            completion_criteria=[
                CompletionCriterion(
                    id="login_gate",
                    outcome="the target path is blocked by a login gate",
                    output_path="output.login_gate_blocks_target",
                    expected_output_value=True,
                    expected_output_shape="goal_judgment_boolean",
                    requested_output_evidence_source="runtime_output",
                )
            ]
        )

        assert workflow_update_module._output_contract_required_paths_source(ctx).union == set()

    def test_non_judgment_scalar_bind_set_is_unchanged(self) -> None:
        ctx = _code_only_ctx()
        ctx.request_policy = RequestPolicy(
            completion_criteria=[
                CompletionCriterion(
                    id="record_id",
                    outcome="the record id is returned",
                    output_path="output.record_id",
                )
            ]
        )

        assert workflow_update_module._output_contract_required_paths_source(ctx).observation_paths == {
            "output.record_id"
        }

    def test_judgment_output_paths_widens_independent_predicate(self) -> None:
        ctx = _code_only_ctx()
        ctx.request_policy = RequestPolicy(
            completion_criteria=[
                CompletionCriterion(
                    id="independent_gate",
                    outcome="the independent path is blocked by a login gate",
                    output_path="output.independent_gate_blocks_target",
                    expected_output_shape="goal_judgment_boolean",
                    requested_output_evidence_source="independent_run_evidence",
                ),
                CompletionCriterion(
                    id="runtime_gate",
                    outcome="the runtime path is blocked by a login gate",
                    output_path="output.runtime_gate_blocks_target",
                    expected_output_value=True,
                    expected_output_shape="goal_judgment_boolean",
                    requested_output_evidence_source="runtime_output",
                ),
            ]
        )

        all_judgment = workflow_update_module._judgment_output_paths(ctx)

        assert all_judgment == {
            "output.independent_gate_blocks_target",
            "output.runtime_gate_blocks_target",
        }

    def test_independent_judgment_runtime_repair_fact_is_not_rehydrated_as_code_return_path(self) -> None:
        ctx = _code_only_ctx()
        ctx.request_policy = RequestPolicy(
            completion_criteria=[
                CompletionCriterion(
                    id="login_gate",
                    outcome="the target path is blocked by a login gate",
                    output_path="output.login_gate_blocks_target",
                    expected_output_value=True,
                    expected_output_shape="goal_judgment_boolean",
                    requested_output_evidence_source="independent_run_evidence",
                )
            ]
        )
        ctx.latest_recorded_build_test_outcome = RecordedBuildTestOutcome(
            phase="persisted_block_run",
            attempted_tool="update_and_run_blocks",
            verdict="repairable_failure",
            reason_code="outcome_not_demonstrated",
            workflow_run_id="wr_current",
            structural_failure_identity="completion:runtime-output",
            runtime_output_repair_facts=[
                {
                    "workflow_run_id": "wr_current",
                    "block_label": "judge_login_gate_blocks_target",
                    "output_path": "output.login_gate_blocks_target",
                    "output_root": "output",
                    "criterion_id": "__copilot_requested_output__output_login_gate_blocks_target",
                    "reason_code": "structurally_abstained",
                    "grounding_mode": "judgment_boolean",
                    "value_status": "structural_abstained",
                }
            ],
        )

        required_paths = workflow_update_module._output_contract_required_paths_source(ctx).union

        assert required_paths == set()

    def test_mixed_runtime_repair_facts_keep_non_judgment_code_return_path(self) -> None:
        ctx = _code_only_ctx()
        ctx.request_policy = RequestPolicy(
            completion_criteria=[
                CompletionCriterion(
                    id="login_gate",
                    outcome="the target path is blocked by a login gate",
                    output_path="output.login_gate_blocks_target",
                    expected_output_value=True,
                    expected_output_shape="goal_judgment_boolean",
                    requested_output_evidence_source="independent_run_evidence",
                ),
                CompletionCriterion(
                    id="record_id",
                    outcome="the record id is returned",
                    output_path="output.record_id",
                ),
            ]
        )
        ctx.latest_recorded_build_test_outcome = RecordedBuildTestOutcome(
            phase="persisted_block_run",
            attempted_tool="update_and_run_blocks",
            verdict="repairable_failure",
            reason_code="outcome_not_demonstrated",
            workflow_run_id="wr_current",
            structural_failure_identity="completion:runtime-output",
            runtime_output_repair_facts=[
                {
                    "workflow_run_id": "wr_current",
                    "block_label": "extract_entry_output",
                    "output_path": "output.login_gate_blocks_target",
                    "output_root": "output",
                    "criterion_id": "__copilot_requested_output__output_login_gate_blocks_target",
                    "reason_code": "structurally_abstained",
                    "grounding_mode": "judgment_boolean",
                    "value_status": "structural_abstained",
                },
                {
                    "workflow_run_id": "wr_current",
                    "block_label": "extract_entry_output",
                    "output_path": "output.record_id",
                    "output_root": "output",
                    "criterion_id": "record_id",
                    "reason_code": "structurally_abstained",
                    "grounding_mode": "missing",
                    "value_status": "structural_abstained",
                },
            ],
        )

        contract = workflow_update_module._output_contract_required_paths_source(ctx)
        required_paths, source, reason_code = contract.union, contract.source, contract.reason_code

        assert required_paths == {"output.record_id"}
        assert source == "runtime_output_repair"
        assert reason_code == "runtime_output_repair_required"

    def test_runtime_output_facts_record_same_run_null_without_evidence_text_backfill(self) -> None:
        result = {
            "ok": True,
            "data": {
                "workflow_run_id": "wr_current",
                "blocks": [
                    {
                        "label": "extract_entry_output",
                        "status": "completed",
                        "extracted_data": {"output": {"npi": None}, "evidence_text": "Value 1234567890"},
                    }
                ],
            },
        }
        verification = CompletionVerificationResult(
            status="evaluated",
            criterion_ids=["requested_npi"],
            verdicts=[
                CriterionVerdict(
                    criterion_id="requested_npi",
                    state="unsatisfied",
                    reason_code="evidence_contradicts",
                    output_path="output.npi",
                    grounding_mode="exact_value",
                    expected_output_shape="string",
                    has_exact_value=False,
                )
            ],
        )

        outcome = recorded_outcome_from_run_blocks_result(
            result,
            recorded_run_outcome=RecordedRunOutcome(
                verdict="not_demonstrated",
                reason_code="outcome_not_demonstrated",
                workflow_run_id="wr_current",
            ),
            completion_verification=verification,
            registered_output_parameter_payloads=[
                {
                    "workflow_run_id": "wr_current",
                    "block_label": "extract_entry_output",
                    "output_parameter_key": "npi",
                    "value": None,
                }
            ],
        )

        assert outcome is not None
        assert outcome.reason_code == "outcome_not_demonstrated"
        assert outcome.runtime_output_repair_facts == [
            {
                "workflow_run_id": "wr_current",
                "block_label": "extract_entry_output",
                "output_path": "output.npi",
                "output_root": "output",
                "criterion_id": "requested_npi",
                "reason_code": "evidence_contradicts",
                "grounding_mode": "exact_value",
                "expected_output_shape": "string",
                "value_status": "null",
                "evidence_refs": ["registered_output:extract_entry_output:npi", "output:extract_entry_output"],
            }
        ]
        assert outcome.is_authoritative is True

    def test_runtime_output_facts_record_empty_typed_output_paths_for_next_contract(self) -> None:
        result = {
            "ok": True,
            "data": {
                "workflow_run_id": "wr_current",
                "blocks": [
                    {
                        "label": "summarize_access_output",
                        "status": "completed",
                        "extracted_data": {
                            "extracted_information": [],
                            "summarize_access_output": {},
                            "evidence_text": "diagnostic page text",
                        },
                    }
                ],
            },
        }
        verification = CompletionVerificationResult(
            status="evaluated",
            criterion_ids=[
                "requested_form_exists",
                "requested_path_label",
                "requested_next_action",
            ],
            verdicts=[
                CriterionVerdict(
                    criterion_id="requested_form_exists",
                    state="unsatisfied",
                    reason_code="structurally_abstained",
                    output_path="output.public_form_exists",
                    grounding_mode="missing",
                    expected_output_shape="boolean",
                ),
                CriterionVerdict(
                    criterion_id="requested_path_label",
                    state="unsatisfied",
                    reason_code="missing_exact_field",
                    output_path="output.visible_page_path_label",
                    grounding_mode="missing",
                    expected_output_shape="string",
                ),
                CriterionVerdict(
                    criterion_id="requested_next_action",
                    state="unsatisfied",
                    reason_code="missing_exact_field",
                    output_path="output.recommended_next_action",
                    grounding_mode="missing",
                    expected_output_shape="string",
                ),
            ],
        )

        outcome = recorded_outcome_from_run_blocks_result(
            result,
            recorded_run_outcome=RecordedRunOutcome(
                verdict="not_demonstrated",
                reason_code="outcome_not_demonstrated",
                workflow_run_id="wr_current",
            ),
            completion_verification=verification,
        )
        assert outcome is not None
        assert {fact["output_path"]: fact["value_status"] for fact in outcome.runtime_output_repair_facts} == {
            "output.public_form_exists": "structural_abstained",
            "output.recommended_next_action": "no_typed_value",
            "output.visible_page_path_label": "no_typed_value",
        }
        assert all(
            "evidence_text" not in ref
            for fact in outcome.runtime_output_repair_facts
            for ref in fact.get("evidence_refs", [])
        )

        ctx = _code_only_ctx()
        ctx.latest_recorded_build_test_outcome = outcome
        contract = workflow_update_module._output_contract_required_paths_source(ctx)
        required_paths, source, reason_code = contract.union, contract.source, contract.reason_code
        assert required_paths == {
            "output.public_form_exists",
            "output.recommended_next_action",
            "output.visible_page_path_label",
        }
        assert source == "runtime_output_repair"
        assert reason_code == "runtime_output_repair_required"

    def test_runtime_output_facts_preserve_satisfied_output_owner_label(self) -> None:
        result = {
            "ok": True,
            "data": {
                "workflow_run_id": "wr_current",
                "blocks": [
                    {
                        "label": "download_statement",
                        "status": "completed",
                        "extracted_data": {"output": {"statement_pdf": "statement.pdf"}},
                    }
                ],
            },
        }
        verification = CompletionVerificationResult(
            status="evaluated",
            criterion_ids=["__copilot_fallback_floor__run", "requested_statement_pdf"],
            verdicts=[
                CriterionVerdict(
                    criterion_id="__copilot_fallback_floor__run",
                    state="unsatisfied",
                    reason_code="no_evidence",
                ),
                CriterionVerdict(
                    criterion_id="requested_statement_pdf",
                    state="satisfied",
                    reason_code="evidence_confirms",
                    output_path="output.statement_pdf",
                    grounding_mode="exact_value",
                    expected_output_shape="string",
                    has_exact_value=True,
                    evidence_ref="block_outputs:download_statement.output.statement_pdf",
                ),
            ],
        )

        outcome = recorded_outcome_from_run_blocks_result(
            result,
            recorded_run_outcome=RecordedRunOutcome(
                verdict="not_demonstrated",
                reason_code="outcome_not_demonstrated",
                workflow_run_id="wr_current",
            ),
            completion_verification=verification,
        )

        assert outcome is not None
        assert outcome.reason_code == "outcome_not_demonstrated"
        assert outcome.runtime_output_repair_facts == [
            {
                "workflow_run_id": "wr_current",
                "block_label": "download_statement",
                "owner_labels": ["download_statement"],
                "output_path": "output.statement_pdf",
                "output_root": "output",
                "criterion_id": "requested_statement_pdf",
                "reason_code": "evidence_confirms",
                "grounding_mode": "exact_value",
                "expected_output_shape": "string",
                "value_status": "satisfied",
                "evidence_refs": ["output:download_statement"],
            }
        ]

    def test_runtime_output_facts_preserve_flat_registered_output_owner_label(self) -> None:
        result = {
            "ok": True,
            "data": {
                "workflow_run_id": "wr_current",
                "blocks": [
                    {"label": "apex_portal_login", "status": "completed", "extracted_data": {}},
                    {"label": "apex_open_monthly_statement", "status": "completed", "extracted_data": {}},
                    {"label": "apex_download_invoice_pdf", "status": "completed", "extracted_data": {}},
                ],
            },
        }
        verification = CompletionVerificationResult(
            status="evaluated",
            criterion_ids=["__copilot_authored_output__output_file_name"],
            verdicts=[
                CriterionVerdict(
                    criterion_id="__copilot_authored_output__output_file_name",
                    state="unsatisfied",
                    reason_code="no_evidence",
                    output_path="output.file_name",
                    grounding_mode="missing",
                    requested_output_evidence_source="runtime_output",
                )
            ],
        )

        outcome = recorded_outcome_from_run_blocks_result(
            result,
            recorded_run_outcome=RecordedRunOutcome(
                verdict="not_demonstrated",
                reason_code="outcome_not_demonstrated",
                workflow_run_id="wr_current",
            ),
            completion_verification=verification,
            registered_output_parameter_payloads=[
                {
                    "workflow_run_id": "wr_current",
                    "block_label": "apex_download_invoice_pdf",
                    "output_parameter_key": "apex_download_invoice_pdf_output",
                    "value": {
                        "file_name": "statement.pdf",
                        "downloaded_files": [{"filename": "statement.pdf"}],
                    },
                }
            ],
        )

        assert outcome is not None
        assert outcome.runtime_output_repair_facts == [
            {
                "workflow_run_id": "wr_current",
                "block_label": "apex_download_invoice_pdf",
                "output_path": "output.file_name",
                "output_root": "output",
                "criterion_id": "__copilot_authored_output__output_file_name",
                "reason_code": "no_evidence",
                "grounding_mode": "missing",
                "value_status": "no_typed_value",
                "evidence_refs": ["registered_output:apex_download_invoice_pdf:apex_download_invoice_pdf_output"],
            }
        ]
        ctx = _code_only_ctx()
        ctx.latest_recorded_build_test_outcome = outcome
        workflow_yaml = _yaml(
            """
            title: Statement download
            workflow_definition:
              blocks:
              - block_type: code
                label: apex_portal_login
                code: |
                  return {"logged_in": True}
              - block_type: code
                label: apex_open_monthly_statement
                code: |
                  return {"matched": True}
              - block_type: code
                label: apex_download_invoice_pdf
                code: |
                  return {"file_name": "statement.pdf"}
            """
        )

        evaluation = workflow_update_module._evaluate_output_contract_for_code_block(ctx, workflow_yaml, [])

        assert evaluation is not None
        assert evaluation.block_label == "apex_download_invoice_pdf"
        assert evaluation.payload["output_owner_labels"] == ["apex_download_invoice_pdf"]
        assert "missing_output_owner" not in evaluation.shape_violations

    def test_runtime_output_facts_do_not_infer_registered_owner_from_parameter_key(self) -> None:
        result = {
            "ok": True,
            "data": {
                "workflow_run_id": "wr_current",
                "blocks": [
                    {"label": "download_invoice", "status": "completed", "extracted_data": {}},
                ],
            },
        }
        verification = CompletionVerificationResult(
            status="evaluated",
            criterion_ids=["__copilot_authored_output__output_file_name"],
            verdicts=[
                CriterionVerdict(
                    criterion_id="__copilot_authored_output__output_file_name",
                    state="unsatisfied",
                    reason_code="no_evidence",
                    output_path="output.file_name",
                    grounding_mode="missing",
                    requested_output_evidence_source="runtime_output",
                )
            ],
        )

        outcome = recorded_outcome_from_run_blocks_result(
            result,
            recorded_run_outcome=RecordedRunOutcome(
                verdict="not_demonstrated",
                reason_code="outcome_not_demonstrated",
                workflow_run_id="wr_current",
            ),
            completion_verification=verification,
            registered_output_parameter_payloads=[
                {
                    "workflow_run_id": "wr_current",
                    "output_parameter_key": "download_invoice_output",
                    "value": {"file_name": "statement.pdf"},
                }
            ],
        )

        assert outcome is not None
        assert outcome.runtime_output_repair_facts == [
            {
                "workflow_run_id": "wr_current",
                "output_path": "output.file_name",
                "output_root": "output",
                "criterion_id": "__copilot_authored_output__output_file_name",
                "reason_code": "no_evidence",
                "grounding_mode": "missing",
                "value_status": "no_typed_value",
                "evidence_refs": ["registered_output:unknown:download_invoice_output"],
            }
        ]
        ctx = _code_only_ctx()
        ctx.latest_recorded_build_test_outcome = outcome
        workflow_yaml = _yaml(
            """
            title: Statement download
            workflow_definition:
              blocks:
              - block_type: code
                label: download_invoice
                code: |
                  return {"file_name": "statement.pdf"}
            """
        )

        evaluation = workflow_update_module._evaluate_output_contract_for_code_block(ctx, workflow_yaml, [])

        assert evaluation is not None
        assert evaluation.block_label == ""
        assert evaluation.payload["output_owner_labels"] == []
        assert "missing_output_owner" in evaluation.shape_violations

    def test_runtime_output_owner_ignores_unsatisfied_independent_self_emitted_fields(self) -> None:
        result = {
            "ok": True,
            "data": {
                "workflow_run_id": "wr_current",
                "blocks": [
                    {
                        "label": "login_to_service",
                        "status": "completed",
                        "extracted_data": {"output": {"logged_in": True}},
                    },
                    {
                        "label": "open_statement",
                        "status": "completed",
                        "extracted_data": {
                            "output": {
                                "matched": True,
                                "statement_date": "2026-05",
                                "visible_page_label": "Statement details",
                            }
                        },
                    },
                ],
            },
        }
        verification = CompletionVerificationResult(
            status="evaluated",
            criterion_ids=[
                "__copilot_authored_output__output_logged_in",
                "__copilot_authored_output__output_matched",
                "__copilot_authored_output__output_statement_date",
                "__copilot_authored_output__output_visible_page_label",
                "__copilot_authored_output__output_downloaded",
            ],
            verdicts=[
                CriterionVerdict(
                    criterion_id="__copilot_authored_output__output_logged_in",
                    state="unsatisfied",
                    reason_code="structurally_abstained",
                    output_path="output.logged_in",
                    grounding_mode="missing",
                    requested_output_evidence_source="independent_run_evidence",
                    evidence_ref="block_outputs:login_to_service.output.logged_in",
                ),
                CriterionVerdict(
                    criterion_id="__copilot_authored_output__output_matched",
                    state="unsatisfied",
                    reason_code="structurally_abstained",
                    output_path="output.matched",
                    grounding_mode="missing",
                    requested_output_evidence_source="independent_run_evidence",
                    evidence_ref="block_outputs:open_statement.output.matched",
                ),
                CriterionVerdict(
                    criterion_id="__copilot_authored_output__output_statement_date",
                    state="unsatisfied",
                    reason_code="structurally_abstained",
                    output_path="output.statement_date",
                    grounding_mode="missing",
                    requested_output_evidence_source="runtime_output",
                    evidence_ref="block_outputs:open_statement.output.statement_date",
                ),
                CriterionVerdict(
                    criterion_id="__copilot_authored_output__output_visible_page_label",
                    state="unsatisfied",
                    reason_code="structurally_abstained",
                    output_path="output.visible_page_label",
                    grounding_mode="missing",
                    requested_output_evidence_source="runtime_output",
                    evidence_ref="block_outputs:open_statement.output.visible_page_label",
                ),
                CriterionVerdict(
                    criterion_id="__copilot_authored_output__output_downloaded",
                    state="unsatisfied",
                    reason_code="no_evidence",
                    output_path="output.downloaded",
                    grounding_mode="missing",
                    requested_output_evidence_source="independent_run_evidence",
                ),
            ],
        )

        outcome = recorded_outcome_from_run_blocks_result(
            result,
            recorded_run_outcome=RecordedRunOutcome(
                verdict="not_demonstrated",
                reason_code="outcome_not_demonstrated",
                workflow_run_id="wr_current",
            ),
            completion_verification=verification,
        )
        ctx = _code_only_ctx()
        ctx.latest_recorded_build_test_outcome = outcome
        workflow_yaml = _yaml(
            """
            title: Statement workflow
            workflow_definition:
              blocks:
              - block_type: code
                label: login_to_service
                code: |
                  return {"output": {"logged_in": True}}
              - block_type: code
                label: open_statement
                code: |
                  return {"output": {"statement_date": "2026-05"}}
            """
        )

        evaluation = workflow_update_module._evaluate_output_contract_for_code_block(ctx, workflow_yaml, [])

        assert outcome is not None
        assert evaluation is not None
        assert evaluation.block_label == ""
        assert evaluation.payload["output_owner_labels"] == []
        assert "missing_output_owner" in evaluation.shape_violations

    def test_runtime_output_facts_ignore_other_run_registered_values(self) -> None:
        result = {
            "ok": True,
            "data": {
                "workflow_run_id": "wr_current",
                "blocks": [{"label": "extract_entry_output", "status": "completed", "extracted_data": {}}],
            },
        }
        verification = CompletionVerificationResult(
            status="evaluated",
            criterion_ids=["requested_npi"],
            verdicts=[
                CriterionVerdict(
                    criterion_id="requested_npi",
                    state="unsatisfied",
                    reason_code="no_evidence",
                    output_path="output.npi",
                    grounding_mode="missing",
                )
            ],
        )

        outcome = recorded_outcome_from_run_blocks_result(
            result,
            recorded_run_outcome=RecordedRunOutcome(
                verdict="not_demonstrated",
                reason_code="outcome_not_demonstrated",
                workflow_run_id="wr_current",
            ),
            completion_verification=verification,
            registered_output_parameter_payloads=[
                {
                    "workflow_run_id": "wr_previous",
                    "block_label": "extract_entry_output",
                    "output_parameter_key": "npi",
                    "value": None,
                }
            ],
        )

        assert outcome is not None
        assert outcome.runtime_output_repair_facts[0]["value_status"] == "no_typed_value"
        assert "registered_output:extract_entry_output:npi" not in outcome.runtime_output_repair_facts[0].get(
            "evidence_refs", []
        )

    def test_runtime_output_facts_ignore_unscoped_registered_values(self) -> None:
        result = {
            "ok": True,
            "data": {
                "workflow_run_id": "wr_current",
                "blocks": [{"label": "extract_entry_output", "status": "completed", "extracted_data": {}}],
            },
        }
        verification = CompletionVerificationResult(
            status="evaluated",
            criterion_ids=["requested_npi"],
            verdicts=[
                CriterionVerdict(
                    criterion_id="requested_npi",
                    state="unsatisfied",
                    reason_code="no_evidence",
                    output_path="output.npi",
                    grounding_mode="missing",
                )
            ],
        )

        outcome = recorded_outcome_from_run_blocks_result(
            result,
            recorded_run_outcome=RecordedRunOutcome(
                verdict="not_demonstrated",
                reason_code="outcome_not_demonstrated",
                workflow_run_id="wr_current",
            ),
            completion_verification=verification,
            registered_output_parameter_payloads=[
                {
                    "block_label": "extract_entry_output",
                    "output_parameter_key": "npi",
                    "value": None,
                }
            ],
        )

        assert outcome is not None
        assert outcome.runtime_output_repair_facts[0]["value_status"] == "no_typed_value"
        assert "block_label" not in outcome.runtime_output_repair_facts[0]
        assert "registered_output:extract_entry_output:npi" not in outcome.runtime_output_repair_facts[0].get(
            "evidence_refs", []
        )

    def test_runtime_output_facts_ignore_unscoped_fallback_registered_values(self) -> None:
        result = {
            "ok": True,
            "data": {
                "workflow_run_id": "wr_current",
                "registered_output_parameter_values": [
                    {
                        "block_label": "extract_entry_output",
                        "output_parameter_key": "npi",
                        "value": None,
                    }
                ],
                "blocks": [{"label": "extract_entry_output", "status": "completed", "extracted_data": {}}],
            },
        }
        verification = CompletionVerificationResult(
            status="evaluated",
            criterion_ids=["requested_npi"],
            verdicts=[
                CriterionVerdict(
                    criterion_id="requested_npi",
                    state="unsatisfied",
                    reason_code="no_evidence",
                    output_path="output.npi",
                    grounding_mode="missing",
                )
            ],
        )

        outcome = recorded_outcome_from_run_blocks_result(
            result,
            recorded_run_outcome=RecordedRunOutcome(
                verdict="not_demonstrated",
                reason_code="outcome_not_demonstrated",
                workflow_run_id="wr_current",
            ),
            completion_verification=verification,
        )

        assert outcome is not None
        assert outcome.runtime_output_repair_facts[0]["value_status"] == "no_typed_value"
        assert "block_label" not in outcome.runtime_output_repair_facts[0]
        assert "registered_output:extract_entry_output:npi" not in outcome.runtime_output_repair_facts[0].get(
            "evidence_refs", []
        )

    def test_runtime_output_facts_override_request_policy_contract_paths(self) -> None:
        ctx = _code_only_ctx()
        ctx.request_policy = RequestPolicy(
            completion_criteria=[
                _typed_completion_criterion(
                    id="requested_value",
                    output_path="output.requested_value",
                    level="run",
                    method_mandated=False,
                    kind="outcome",
                )
            ]
        )
        ctx.latest_recorded_build_test_outcome = RecordedBuildTestOutcome(
            phase="persisted_block_run",
            attempted_tool="update_and_run_blocks",
            verdict="repairable_failure",
            reason_code="outcome_not_demonstrated",
            workflow_run_id="wr_current",
            structural_failure_identity="completion:runtime-output",
            runtime_output_repair_facts=[
                {
                    "workflow_run_id": "wr_current",
                    "block_label": "extract_entry_output",
                    "output_path": "output.npi",
                    "output_root": "output",
                    "criterion_id": "requested_npi",
                    "reason_code": "evidence_contradicts",
                    "value_status": "null",
                },
                {
                    "workflow_run_id": "wr_current",
                    "block_label": "extract_entry_output",
                    "output_path": "output.locations[].address",
                    "output_root": "output",
                    "criterion_id": "requested_location",
                    "reason_code": "structurally_abstained",
                    "grounding_mode": "missing",
                    "value_status": "structural_abstained",
                },
            ],
        )
        workflow_yaml = _yaml(
            """
            title: Entry lookup
            workflow_definition:
              blocks:
              - block_type: code
                label: extract_entry_output
                code: |
                  return {"output": {"requested_value": "wrong"}}
            """
        )

        evaluation = workflow_update_module._evaluate_output_contract_for_code_block(ctx, workflow_yaml, [])

        assert evaluation is not None
        assert evaluation.source == "runtime_output_repair"
        assert evaluation.reason_code == "runtime_output_repair_required"
        assert evaluation.required_paths == {"output.locations[].address", "output.npi"}
        assert sorted(
            evaluation.payload["runtime_output_repair_facts"],
            key=lambda item: str(item.get("output_path") or ""),
        ) == sorted(
            ctx.latest_recorded_build_test_outcome.runtime_output_repair_facts,
            key=lambda item: str(item.get("output_path") or ""),
        )

    def test_runtime_output_owner_selects_current_block_without_metadata(self) -> None:
        ctx = _code_only_ctx()
        ctx.latest_recorded_build_test_outcome = RecordedBuildTestOutcome(
            phase="persisted_block_run",
            attempted_tool="update_and_run_blocks",
            verdict="repairable_failure",
            reason_code="outcome_not_demonstrated",
            workflow_run_id="wr_current",
            structural_failure_identity="completion:runtime-output",
            runtime_output_repair_facts=[
                {
                    "workflow_run_id": "wr_current",
                    "owner_labels": ["download_statement"],
                    "output_path": "output.statement_pdf",
                    "output_root": "output",
                    "criterion_id": "requested_statement_pdf",
                    "reason_code": "evidence_confirms",
                    "value_status": "satisfied",
                }
            ],
        )
        workflow_yaml = _yaml(
            """
            title: Statement download
            workflow_definition:
              blocks:
              - block_type: code
                label: login
                code: |
                  return {"logged_in": True}
              - block_type: code
                label: download_statement
                code: |
                  return {"output": {"statement_pdf": "statement.pdf"}}
            """
        )

        evaluation = workflow_update_module._evaluate_output_contract_for_code_block(ctx, workflow_yaml, [])

        assert evaluation is not None
        assert evaluation.block_label == "download_statement"
        assert evaluation.payload["output_owner_labels"] == ["download_statement"]
        assert "missing_output_owner" not in evaluation.shape_violations

    def test_runtime_output_zero_owner_does_not_fall_through_to_single_block_default(self) -> None:
        ctx = _code_only_ctx()
        ctx.latest_recorded_build_test_outcome = RecordedBuildTestOutcome(
            phase="persisted_block_run",
            attempted_tool="update_and_run_blocks",
            verdict="repairable_failure",
            reason_code="outcome_not_demonstrated",
            workflow_run_id="wr_current",
            structural_failure_identity="completion:runtime-output",
            runtime_output_repair_facts=[
                {
                    "workflow_run_id": "wr_current",
                    "output_path": "output.statement_pdf",
                    "output_root": "output",
                    "criterion_id": "requested_statement_pdf",
                    "reason_code": "evidence_confirms",
                    "value_status": "satisfied",
                }
            ],
        )
        workflow_yaml = _yaml(
            """
            title: Statement download
            workflow_definition:
              blocks:
              - block_type: code
                label: only_block
                code: |
                  return {"output": {"statement_pdf": "statement.pdf"}}
            """
        )

        evaluation = workflow_update_module._evaluate_output_contract_for_code_block(ctx, workflow_yaml, [])

        assert evaluation is not None
        assert evaluation.block_label == ""
        assert evaluation.payload["output_owner_labels"] == []
        assert "missing_output_owner" in evaluation.shape_violations

    def test_runtime_output_multi_owner_rejects_without_picking_metadata_owner(self) -> None:
        ctx = _code_only_ctx()
        ctx.latest_recorded_build_test_outcome = RecordedBuildTestOutcome(
            phase="persisted_block_run",
            attempted_tool="update_and_run_blocks",
            verdict="repairable_failure",
            reason_code="outcome_not_demonstrated",
            workflow_run_id="wr_current",
            structural_failure_identity="completion:runtime-output",
            runtime_output_repair_facts=[
                {
                    "workflow_run_id": "wr_current",
                    "owner_labels": ["download_a", "download_b"],
                    "output_path": "output.statement_pdf",
                    "output_root": "output",
                    "criterion_id": "requested_statement_pdf",
                    "reason_code": "evidence_confirms",
                    "value_status": "satisfied",
                }
            ],
        )
        workflow_yaml = _yaml(
            """
            title: Statement download
            workflow_definition:
              blocks:
              - block_type: code
                label: download_a
                code: |
                  return {"output": {"statement_pdf": "a.pdf"}}
              - block_type: code
                label: download_b
                code: |
                  return {"output": {"statement_pdf": "b.pdf"}}
            """
        )
        metadata = [{"block_label": "download_a", "claimed_outcomes": [{"goal_value_paths": ["output.statement_pdf"]}]}]

        evaluation = workflow_update_module._evaluate_output_contract_for_code_block(ctx, workflow_yaml, metadata)

        assert evaluation is not None
        assert evaluation.block_label == ""
        assert evaluation.payload["output_owner_labels"] == ["download_a", "download_b"]
        assert "ambiguous_output_owner" in evaluation.shape_violations

    def test_runtime_output_stale_multi_owner_rejects_when_one_owner_current(self) -> None:
        ctx = _code_only_ctx()
        ctx.latest_recorded_build_test_outcome = RecordedBuildTestOutcome(
            phase="persisted_block_run",
            attempted_tool="update_and_run_blocks",
            verdict="repairable_failure",
            reason_code="outcome_not_demonstrated",
            workflow_run_id="wr_current",
            structural_failure_identity="completion:runtime-output",
            runtime_output_repair_facts=[
                {
                    "workflow_run_id": "wr_current",
                    "owner_labels": ["current_download", "stale_download"],
                    "output_path": "output.statement_pdf",
                    "output_root": "output",
                    "criterion_id": "requested_statement_pdf",
                    "reason_code": "evidence_confirms",
                    "value_status": "satisfied",
                }
            ],
        )
        workflow_yaml = _yaml(
            """
            title: Statement download
            workflow_definition:
              blocks:
              - block_type: code
                label: current_download
                code: |
                  return {"output": {"statement_pdf": "a.pdf"}}
            """
        )

        evaluation = workflow_update_module._evaluate_output_contract_for_code_block(ctx, workflow_yaml, [])

        assert evaluation is not None
        assert evaluation.block_label == ""
        assert evaluation.payload["output_owner_labels"] == ["current_download"]
        assert "ambiguous_output_owner" in evaluation.shape_violations

    def test_runtime_output_multi_owner_all_stale_rejects_as_missing_owner(self) -> None:
        ctx = _code_only_ctx()
        ctx.latest_recorded_build_test_outcome = RecordedBuildTestOutcome(
            phase="persisted_block_run",
            attempted_tool="update_and_run_blocks",
            verdict="repairable_failure",
            reason_code="outcome_not_demonstrated",
            workflow_run_id="wr_current",
            structural_failure_identity="completion:runtime-output",
            runtime_output_repair_facts=[
                {
                    "workflow_run_id": "wr_current",
                    "owner_labels": ["stale_one", "stale_two"],
                    "output_path": "output.statement_pdf",
                    "output_root": "output",
                    "criterion_id": "requested_statement_pdf",
                    "reason_code": "evidence_confirms",
                    "value_status": "satisfied",
                }
            ],
        )
        workflow_yaml = _yaml(
            """
            title: Statement download
            workflow_definition:
              blocks:
              - block_type: code
                label: current_download
                code: |
                  return {"output": {"statement_pdf": "a.pdf"}}
            """
        )

        evaluation = workflow_update_module._evaluate_output_contract_for_code_block(ctx, workflow_yaml, [])

        assert evaluation is not None
        assert evaluation.block_label == ""
        assert evaluation.payload["output_owner_labels"] == []
        assert "missing_output_owner" in evaluation.shape_violations

    def test_runtime_output_paths_with_disagreeing_single_owners_reject(self) -> None:
        ctx = _code_only_ctx()
        ctx.latest_recorded_build_test_outcome = RecordedBuildTestOutcome(
            phase="persisted_block_run",
            attempted_tool="update_and_run_blocks",
            verdict="repairable_failure",
            reason_code="outcome_not_demonstrated",
            workflow_run_id="wr_current",
            structural_failure_identity="completion:runtime-output",
            runtime_output_repair_facts=[
                {
                    "workflow_run_id": "wr_current",
                    "owner_labels": ["download_pdf"],
                    "output_path": "output.statement_pdf",
                    "output_root": "output",
                    "criterion_id": "requested_statement_pdf",
                    "reason_code": "evidence_confirms",
                    "value_status": "satisfied",
                },
                {
                    "workflow_run_id": "wr_current",
                    "owner_labels": ["extract_total"],
                    "output_path": "output.statement_total",
                    "output_root": "output",
                    "criterion_id": "requested_statement_total",
                    "reason_code": "evidence_confirms",
                    "value_status": "satisfied",
                },
            ],
        )
        workflow_yaml = _yaml(
            """
            title: Statement download
            workflow_definition:
              blocks:
              - block_type: code
                label: download_pdf
                code: |
                  return {"output": {"statement_pdf": "a.pdf"}}
              - block_type: code
                label: extract_total
                code: |
                  return {"output": {"statement_total": "12.00"}}
            """
        )

        evaluation = workflow_update_module._evaluate_output_contract_for_code_block(ctx, workflow_yaml, [])

        assert evaluation is not None
        assert evaluation.block_label == ""
        assert evaluation.payload["output_owner_labels"] == ["download_pdf", "extract_total"]
        assert "ambiguous_output_owner" in evaluation.shape_violations

    def test_runtime_output_owner_overrides_stale_pin(self) -> None:
        ctx = _code_only_ctx()
        ctx.turn_id = "runtime-owner-pin"
        ctx.latest_recorded_build_test_outcome = RecordedBuildTestOutcome(
            phase="persisted_block_run",
            attempted_tool="update_and_run_blocks",
            verdict="repairable_failure",
            reason_code="outcome_not_demonstrated",
            workflow_run_id="wr_current",
            structural_failure_identity="completion:runtime-output",
            runtime_output_repair_facts=[
                {
                    "workflow_run_id": "wr_current",
                    "owner_labels": ["new_owner"],
                    "output_path": "output.statement_pdf",
                    "output_root": "output",
                    "criterion_id": "requested_statement_pdf",
                    "reason_code": "evidence_confirms",
                    "value_status": "satisfied",
                }
            ],
        )
        workflow_yaml = _yaml(
            """
            title: Statement download
            workflow_definition:
              blocks:
              - block_type: code
                label: stale_owner
                code: |
                  return {"output": {"statement_pdf": "old.pdf"}}
              - block_type: code
                label: new_owner
                code: |
                  return {"output": {"statement_pdf": "new.pdf"}}
            """
        )
        pin_key = workflow_update_module._output_contract_pin_key(ctx, workflow_yaml, {"output.statement_pdf"})
        ctx.output_contract_pinned_block_label_by_signature = {pin_key: "stale_owner"}

        evaluation = workflow_update_module._evaluate_output_contract_for_code_block(ctx, workflow_yaml, [])

        assert evaluation is not None
        assert evaluation.block_label == "new_owner"
        assert evaluation.payload["output_owner_labels"] == ["new_owner"]

    @pytest.mark.asyncio
    async def test_runtime_output_repair_facts_trigger_first_contact_envelope(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        ctx.latest_recorded_build_test_outcome = RecordedBuildTestOutcome(
            phase="persisted_block_run",
            attempted_tool="update_and_run_blocks",
            verdict="repairable_failure",
            reason_code="outcome_not_demonstrated",
            workflow_run_id="wr_current",
            structural_failure_identity="completion:runtime-output",
            runtime_output_repair_facts=[
                {
                    "workflow_run_id": "wr_current",
                    "block_label": "extract_entry_output",
                    "output_path": "output.npi",
                    "output_root": "output",
                    "criterion_id": "requested_npi",
                    "reason_code": "evidence_contradicts",
                    "value_status": "null",
                }
            ],
        )
        workflow_yaml = _yaml(
            """
            title: Entry lookup
            workflow_definition:
              blocks:
              - block_type: code
                label: extract_entry_output
                code: |
                  npi = "1234567890"
                  return npi
            """
        )

        result = await _update_workflow({"workflow_yaml": workflow_yaml}, ctx, allow_missing_credentials=True)

        assert result["ok"] is True
        parsed = parse_workflow_yaml(ctx.workflow_yaml)
        code = _single_code_block(parsed)["code"]
        assert 'return {"output": {"npi": npi}}' in code
        assert ctx.code_artifact_metadata["extract_entry_output"]["claimed_outcomes"][0]["goal_value_paths"] == [
            "output.npi"
        ]
        assert ctx.runtime_output_repair_attempt_by_signature

    @pytest.mark.asyncio
    async def test_update_workflow_applies_metadata_contract_scaffold_for_unambiguous_owner(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        ctx.request_policy = RequestPolicy(
            completion_criteria=[
                _typed_completion_criterion(
                    id="requested_value",
                    output_path="output.record_id",
                    level="run",
                    method_mandated=False,
                    kind="outcome",
                ),
                _typed_completion_criterion(
                    id="requested_flags",
                    output_path="output.flags",
                    level="run",
                    method_mandated=False,
                    kind="outcome",
                ),
            ]
        )
        workflow_yaml = _yaml(
            """
            title: Entry lookup
            workflow_definition:
              blocks:
              - block_type: code
                label: extract_entry_output
                code: |
                  return {"output": {"record_id": "ABC123", "flags": ["enabled"]}}
            """
        )

        result = await _update_workflow({"workflow_yaml": workflow_yaml}, ctx, allow_missing_credentials=True)

        assert result["ok"] is True
        stored = ctx.code_artifact_metadata["extract_entry_output"]
        assert stored["artifact_id"] == "code_artifact:extract_entry_output"
        assert stored["claimed_outcomes"][0]["goal_value_paths"] == ["output.flags", "output.record_id"]
        assert stored["terminal_verifier_expectations"][0]["goal_value_paths"] == [
            "output.flags",
            "output.record_id",
        ]
        schema = json.loads(stored["claimed_outcomes"][0]["extraction_schema"])
        assert schema["properties"]["output"]["properties"]["record_id"] == {}
        assert schema["properties"]["output"]["properties"]["flags"] == {}
        assert ctx.workflow_verification_evidence.code_artifact_metadata == ctx.code_artifact_metadata

    @pytest.mark.asyncio
    async def test_separated_spine_shape_allows_neutral_multi_block_output_owner(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        ctx.scout_trajectory = [
            {"tool_name": "type_text", "selector": "#filter", "source_url": "https://example.com/records"},
            {"tool_name": "click", "selector": "#choose", "source_url": "https://example.com/records"},
        ]
        ctx.request_policy = RequestPolicy(
            completion_criteria=[
                _typed_completion_criterion(
                    id="requested_value",
                    output_path="output.record_id",
                    level="run",
                    method_mandated=False,
                    kind="outcome",
                )
            ]
        )
        schema = (
            '{"type":"object","properties":{"output":{"type":"object","properties":{"record_id":{"type":"string"}}}}}'
        )
        workflow_yaml = _yaml(
            """
            title: Entry lookup
            workflow_definition:
              blocks:
              - block_type: code
                label: enter_filters
                code: |
                  await page.locator("#filter").fill("ABC123")
              - block_type: code
                label: choose_record
                code: |
                  await page.locator("#choose").click()
              - block_type: code
                label: extract_record
                code: |
                  return {"output": {"record_id": "ABC123"}}
            """
        )
        metadata = [
            {
                "block_label": "extract_record",
                "claimed_outcomes": [{"goal_value_paths": ["output.record_id"], "extraction_schema": schema}],
                "terminal_verifier_expectations": [
                    {"goal_value_paths": ["output.record_id"], "extraction_schema": schema}
                ],
            }
        ]

        result = await _update_workflow(
            {"workflow_yaml": workflow_yaml, "code_artifact_metadata": metadata},
            ctx,
            allow_missing_credentials=True,
        )

        assert result["ok"] is True
        labels = [block.get("label") for block in workflow_blocks(parse_workflow_yaml(ctx.workflow_yaml))]
        assert labels == ["enter_filters", "choose_record", "extract_record"]

    @pytest.mark.asyncio
    async def test_separated_spine_shape_ignores_one_block_without_multi_stage_spine(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        ctx.scout_trajectory = []
        ctx.request_policy = RequestPolicy(
            completion_criteria=[
                _typed_completion_criterion(
                    id="requested_value",
                    output_path="output.record_id",
                    level="run",
                    method_mandated=False,
                    kind="outcome",
                )
            ]
        )
        schema = (
            '{"type":"object","properties":{"output":{"type":"object","properties":{"record_id":{"type":"string"}}}}}'
        )
        workflow_yaml = _yaml(
            """
            title: Entry lookup
            workflow_definition:
              blocks:
              - block_type: code
                label: extract_record
                code: |
                  return {"output": {"record_id": "ABC123"}}
            """
        )
        metadata = [
            {
                "block_label": "extract_record",
                "claimed_outcomes": [{"goal_value_paths": ["output.record_id"], "extraction_schema": schema}],
                "terminal_verifier_expectations": [
                    {"goal_value_paths": ["output.record_id"], "extraction_schema": schema}
                ],
            }
        ]

        result = await _update_workflow(
            {"workflow_yaml": workflow_yaml, "code_artifact_metadata": metadata},
            ctx,
            allow_missing_credentials=True,
        )

        assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_requested_output_contract_allows_aligned_initial_candidate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        label = "lookup_entry"
        child_paths = ["output.npi", "output.locations[].address", "output.statuses"]
        schema = (
            '{"type":"object","properties":{"output":{"type":"object","properties":{'
            '"npi":{"type":"string"},'
            '"locations":{"type":"array","items":{"type":"object","properties":{"address":{"type":"string"}}}},'
            '"statuses":{"type":"array","items":{"type":"string"}}}}}}'
        )
        ctx.request_policy = RequestPolicy(
            completion_criteria=[
                _typed_completion_criterion(
                    id=f"requested_{index}",
                    output_path=path,
                    level="run",
                    method_mandated=False,
                    kind="outcome",
                )
                for index, path in enumerate(child_paths)
            ]
        )
        ctx.code_artifact_metadata = {
            label: {
                "block_label": label,
                "claimed_outcomes": [{"goal_value_paths": child_paths, "extraction_schema": schema}],
                "terminal_verifier_expectations": [{"goal_value_paths": child_paths, "extraction_schema": schema}],
            }
        }
        ctx.workflow_verification_evidence.code_artifact_metadata = dict(ctx.code_artifact_metadata)
        candidate_yaml = _yaml(
            f"""
            title: Entry lookup
            workflow_definition:
              blocks:
              - block_type: code
                label: {label}
                code: |
                  return {{
                      "output": {{
                          "npi": "1234567890",
                          "locations": [{{"address": "Example location"}}],
                          "statuses": ["active"],
                      }}
                  }}
            """
        )

        result = await _update_workflow({"workflow_yaml": candidate_yaml}, ctx, allow_missing_credentials=True)

        assert result["ok"] is True
        labels = [block.get("label") for block in workflow_blocks(parse_workflow_yaml(ctx.workflow_yaml))]
        assert labels == [label]

    @pytest.mark.asyncio
    async def test_authoritative_outcome_not_demonstrated_allows_static_return_for_required_root(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        label = "extract_top_hn_post"
        ctx.code_artifact_metadata = {
            label: {
                "block_label": label,
                "claimed_outcomes": [{"goal_value_paths": ["top_post"]}],
                "terminal_verifier_expectations": [{"goal_value_paths": ["top_post"]}],
            }
        }
        ctx.workflow_verification_evidence.code_artifact_metadata = dict(ctx.code_artifact_metadata)
        ctx.latest_recorded_build_test_outcome = RecordedBuildTestOutcome(
            phase="persisted_block_run",
            attempted_tool="update_and_run_blocks",
            verdict="repairable_failure",
            reason_code="outcome_not_demonstrated",
            structural_failure_identity="completion:unsatisfied-output",
            authored_structure_signature="authored:previous-failed-candidate",
            missing_requested_output_facts=[
                {"output_path": "top_post", "output_root": "top_post", "value_status": "presence_only_evidence"},
            ],
        )
        candidate_yaml = _yaml(
            f"""
            title: Hacker News lookup
            workflow_definition:
              blocks:
              - block_type: code
                label: {label}
                code: |
                  return {{"top_post": "Claude Sonnet 5", "rank": 1}}
            """
        )

        result = await _update_workflow({"workflow_yaml": candidate_yaml}, ctx, allow_missing_credentials=True)

        assert result["ok"] is True
        assert ctx.workflow_yaml == candidate_yaml

    @pytest.mark.asyncio
    async def test_authoritative_outcome_not_demonstrated_allows_candidate_covering_missing_output_roots(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        label = "lookup_provider_and_extract_credentials"
        ctx.code_artifact_metadata = {
            label: {
                "block_label": label,
                "claimed_outcomes": [
                    {"goal_value_paths": ["address", "credentialing_status", "locations", "statuses"]}
                ],
                "terminal_verifier_expectations": [
                    {"goal_value_paths": ["address", "credentialing_status", "locations", "statuses"]}
                ],
            }
        }
        ctx.workflow_verification_evidence.code_artifact_metadata = dict(ctx.code_artifact_metadata)
        ctx.latest_recorded_build_test_outcome = RecordedBuildTestOutcome(
            phase="persisted_block_run",
            attempted_tool="update_and_run_blocks",
            verdict="repairable_failure",
            reason_code="outcome_not_demonstrated",
            structural_failure_identity="completion:unsatisfied-output",
            authored_structure_signature="authored:previous-failed-candidate",
            missing_requested_output_facts=[
                {"output_path": "address", "output_root": "address", "value_status": "no_typed_value"},
                {
                    "output_path": "credentialing_status",
                    "output_root": "credentialing_status",
                    "value_status": "no_typed_value",
                },
                {"output_path": "locations", "output_root": "locations", "value_status": "empty_typed_value"},
                {"output_path": "statuses", "output_root": "statuses", "value_status": "no_typed_value"},
            ],
        )
        candidate_yaml = _yaml(
            f"""
            title: Provider lookup
            workflow_definition:
              blocks:
              - block_type: code
                label: {label}
                code: |
                  await page.locator("#locInput").wait_for(state="visible", timeout=15000)
                  address = "North Carolina, USA"
                  credentialing_status = "unknown"
                  locations = []
                  statuses = []
            """
        )

        result = await _update_workflow({"workflow_yaml": candidate_yaml}, ctx, allow_missing_credentials=True)

        assert result["ok"] is True
        assert ctx.workflow_yaml == candidate_yaml

    @pytest.mark.asyncio
    async def test_authoritative_outcome_not_demonstrated_allows_helper_returning_missing_output_roots(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        label = "lookup_provider_and_extract_credentials"
        ctx.code_artifact_metadata = {
            label: {
                "block_label": label,
                "claimed_outcomes": [{"goal_value_paths": ["address", "credentialing_status"]}],
                "terminal_verifier_expectations": [{"goal_value_paths": ["address", "credentialing_status"]}],
            }
        }
        ctx.workflow_verification_evidence.code_artifact_metadata = dict(ctx.code_artifact_metadata)
        ctx.latest_recorded_build_test_outcome = RecordedBuildTestOutcome(
            phase="persisted_block_run",
            attempted_tool="update_and_run_blocks",
            verdict="repairable_failure",
            reason_code="outcome_not_demonstrated",
            structural_failure_identity="completion:unsatisfied-output",
            authored_structure_signature="authored:previous-failed-candidate",
            missing_requested_output_facts=[
                {"output_path": "address", "output_root": "address", "value_status": "no_typed_value"},
                {
                    "output_path": "credentialing_status",
                    "output_root": "credentialing_status",
                    "value_status": "no_typed_value",
                },
            ],
        )
        candidate_yaml = _yaml(
            f"""
            title: Provider lookup
            workflow_definition:
              blocks:
              - block_type: code
                label: {label}
                code: |
                  async def extract():
                      return {{"address": "North Carolina", "credentialing_status": "unknown"}}

                  return await extract()
            """
        )

        result = await _update_workflow({"workflow_yaml": candidate_yaml}, ctx, allow_missing_credentials=True)

        assert result["ok"] is True
        assert ctx.workflow_yaml == candidate_yaml

    @pytest.mark.asyncio
    async def test_authoritative_outcome_not_demonstrated_allows_helper_local_dict_output_roots(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        label = "lookup_provider_and_extract_credentials"
        ctx.code_artifact_metadata = {
            label: {
                "block_label": label,
                "claimed_outcomes": [{"goal_value_paths": ["address", "credentialing_status"]}],
                "terminal_verifier_expectations": [{"goal_value_paths": ["address", "credentialing_status"]}],
            }
        }
        ctx.workflow_verification_evidence.code_artifact_metadata = dict(ctx.code_artifact_metadata)
        ctx.latest_recorded_build_test_outcome = RecordedBuildTestOutcome(
            phase="persisted_block_run",
            attempted_tool="update_and_run_blocks",
            verdict="repairable_failure",
            reason_code="outcome_not_demonstrated",
            structural_failure_identity="completion:unsatisfied-output",
            authored_structure_signature="authored:previous-failed-candidate",
            missing_requested_output_facts=[
                {"output_path": "address", "output_root": "address", "value_status": "no_typed_value"},
                {
                    "output_path": "credentialing_status",
                    "output_root": "credentialing_status",
                    "value_status": "no_typed_value",
                },
            ],
        )
        candidate_yaml = _yaml(
            f"""
            title: Provider lookup
            workflow_definition:
              blocks:
              - block_type: code
                label: {label}
                code: |
                  async def extract():
                      result = {{"address": "North Carolina", "credentialing_status": "unknown"}}
                      return result

                  return await extract()
            """
        )

        result = await _update_workflow({"workflow_yaml": candidate_yaml}, ctx, allow_missing_credentials=True)

        assert result["ok"] is True
        assert ctx.workflow_yaml == candidate_yaml

    @pytest.mark.asyncio
    async def test_authoritative_outcome_not_demonstrated_allows_top_level_literal_key_dict_updates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        label = "lookup_provider_and_extract_credentials"
        ctx.code_artifact_metadata = {
            label: {
                "block_label": label,
                "claimed_outcomes": [{"goal_value_paths": ["address", "credentialing_status"]}],
                "terminal_verifier_expectations": [{"goal_value_paths": ["address", "credentialing_status"]}],
            }
        }
        ctx.workflow_verification_evidence.code_artifact_metadata = dict(ctx.code_artifact_metadata)
        ctx.latest_recorded_build_test_outcome = RecordedBuildTestOutcome(
            phase="persisted_block_run",
            attempted_tool="update_and_run_blocks",
            verdict="repairable_failure",
            reason_code="outcome_not_demonstrated",
            structural_failure_identity="completion:unsatisfied-output",
            authored_structure_signature="authored:previous-failed-candidate",
            missing_requested_output_facts=[
                {"output_path": "address", "output_root": "address", "value_status": "no_typed_value"},
                {
                    "output_path": "credentialing_status",
                    "output_root": "credentialing_status",
                    "value_status": "no_typed_value",
                },
            ],
        )
        candidate_yaml = _yaml(
            f"""
            title: Provider lookup
            workflow_definition:
              blocks:
              - block_type: code
                label: {label}
                code: |
                  result = {{"address": "North Carolina"}}
                  result["credentialing_status"] = "unknown"
                  return result
            """
        )

        result = await _update_workflow({"workflow_yaml": candidate_yaml}, ctx, allow_missing_credentials=True)

        assert result["ok"] is True
        assert ctx.workflow_yaml == candidate_yaml

    @pytest.mark.asyncio
    async def test_authoritative_outcome_not_demonstrated_allows_helper_literal_key_dict_updates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        label = "lookup_provider_and_extract_credentials"
        ctx.code_artifact_metadata = {
            label: {
                "block_label": label,
                "claimed_outcomes": [{"goal_value_paths": ["address", "credentialing_status"]}],
                "terminal_verifier_expectations": [{"goal_value_paths": ["address", "credentialing_status"]}],
            }
        }
        ctx.workflow_verification_evidence.code_artifact_metadata = dict(ctx.code_artifact_metadata)
        ctx.latest_recorded_build_test_outcome = RecordedBuildTestOutcome(
            phase="persisted_block_run",
            attempted_tool="update_and_run_blocks",
            verdict="repairable_failure",
            reason_code="outcome_not_demonstrated",
            structural_failure_identity="completion:unsatisfied-output",
            authored_structure_signature="authored:previous-failed-candidate",
            missing_requested_output_facts=[
                {"output_path": "address", "output_root": "address", "value_status": "no_typed_value"},
                {
                    "output_path": "credentialing_status",
                    "output_root": "credentialing_status",
                    "value_status": "no_typed_value",
                },
            ],
        )
        candidate_yaml = _yaml(
            f"""
            title: Provider lookup
            workflow_definition:
              blocks:
              - block_type: code
                label: {label}
                code: |
                  async def extract():
                      result = {{}}
                      result["address"] = "North Carolina"
                      result["credentialing_status"] = "unknown"
                      return result

                  return await extract()
            """
        )

        result = await _update_workflow({"workflow_yaml": candidate_yaml}, ctx, allow_missing_credentials=True)

        assert result["ok"] is True
        assert ctx.workflow_yaml == candidate_yaml

    @pytest.mark.asyncio
    async def test_captured_select_option_allows_authored_select_api(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        ctx.scout_trajectory = [
            {
                "tool_name": "select_option",
                "selector": "#planSelect",
                "source_url": "https://example.com/plans",
                "value": "gold",
                "trajectory_index": 0,
            }
        ]
        submitted = _yaml(
            """
            title: Plan lookup
            workflow_definition:
              parameters:
              - parameter_type: workflow
                workflow_parameter_type: string
                key: plan
              blocks:
              - block_type: code
                label: plan_selection
                parameter_keys:
                - plan
                code: |
                  await page.locator("#planSelect").select_option(label=str(plan))
                  return {"selected_plan": str(plan)}
            """
        )

        result = await _update_workflow({"workflow_yaml": submitted}, ctx, allow_missing_credentials=True)

        assert result["ok"] is True
        assert ctx.workflow_yaml == submitted
        assert ctx.last_code_authoring_repair_context is None

    @pytest.mark.asyncio
    async def test_captured_select_option_allows_later_unrelated_text_click(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        ctx.scout_trajectory = [
            {
                "tool_name": "select_option",
                "selector": "#planSelect",
                "source_url": "https://example.com/plans",
                "value": "gold",
                "trajectory_index": 0,
            }
        ]
        submitted = _yaml(
            """
            title: Plan lookup
            workflow_definition:
              parameters:
              - parameter_type: workflow
                workflow_parameter_type: string
                key: plan
              blocks:
              - block_type: code
                label: plan_selection
                parameter_keys:
                - plan
                code: |
                  await page.locator("#planSelect").select_option(label=str(plan))
                  return {"selected_plan": str(plan)}
              - block_type: code
                label: continue_from_plan
                code: |
                  await page.get_by_text("Continue", exact=True).click()
                  return {"continued": True}
            """
        )

        result = await _update_workflow({"workflow_yaml": submitted}, ctx, allow_missing_credentials=True)

        assert result["ok"] is True
        assert ctx.workflow_yaml == submitted
        assert ctx.last_code_authoring_repair_context is None

    @pytest.mark.asyncio
    async def test_credential_scout_reject_is_not_classified_as_progress(self) -> None:
        ctx = _code_only_ctx()
        ctx.scout_trajectory = []
        result = await _update_workflow(
            {
                "workflow_yaml": _credential_code_yaml(
                    code="""
                    await page.locator("#email").fill(login_credential.username)
                    await page.locator("input[type='password']").fill(login_credential.password)
                    await page.locator("#totpmfa").fill(login_credential.totp)
                    await page.locator("input[type='submit']").click()
                    await page.wait_for_load_state("load")
                    """
                )
            },
            ctx,
        )
        assert result["ok"] is False
        assert result["data"]["failure_type"] == "missing_credential_or_init"
        assert result["data"].get("surface_kind") is None


_SPINE_SYNTH_CODE = 'await page.locator("#stage-a").click()\nawait page.locator("#stage-b").click()'


def _fake_spine_synthesized(
    *,
    parameters: list[dict[str, str]] | None = None,
    steps: list[dict[str, int]] | None = None,
    code: str | None = None,
    diagnostics: SynthesisDiagnostics | None = None,
) -> SynthesizedCodeBlock:
    return SynthesizedCodeBlock(
        code=code if code is not None else _SPINE_SYNTH_CODE,
        parameters=parameters if parameters is not None else [],
        steps=steps if steps is not None else [{"line_start": 1, "line_end": 1}, {"line_start": 2, "line_end": 2}],
        diagnostics=diagnostics if diagnostics is not None else SynthesisDiagnostics(),
    )


def _spine_emission_diagnostics() -> SynthesisDiagnostics:
    return SynthesisDiagnostics(
        emitted_interaction_count=2,
        emitted_interactions=[
            {
                "trajectory_index": 0,
                "tool_name": "click",
                "method": "click",
                "selector": "#stage-a",
                "locator": 'page.locator("#stage-a")',
            },
            {
                "trajectory_index": 1,
                "tool_name": "click",
                "method": "click",
                "selector": "#stage-b",
                "locator": 'page.locator("#stage-b")',
            },
        ],
    )


def _spine_actuation_ctx() -> CopilotContext:
    ctx = _code_only_ctx()
    ctx.turn_id = "t-spine"
    ctx.scout_trajectory = [
        {"tool_name": "click", "selector": "#stage-a", "source_url": "https://example.com/records"},
        {"tool_name": "click", "selector": "#stage-b", "source_url": "https://example.com/records"},
    ]
    ctx.request_policy = RequestPolicy(
        completion_criteria=[
            _typed_completion_criterion(
                id="requested_value",
                output_path="output.record_id",
                level="run",
                method_mandated=False,
                kind="outcome",
            )
        ]
    )
    return ctx


def _collapsed_spine_yaml(code_body: str) -> str:
    indented = textwrap.indent(textwrap.dedent(code_body).strip(), " " * 10)
    return _yaml(
        "title: Entry lookup\n"
        "workflow_definition:\n"
        "  blocks:\n"
        "  - block_type: code\n"
        "    label: extract_record\n"
        "    code: |\n"
        f"{indented}\n"
    )


class TestSeparatedSpineViolationActuation:
    def test_branch_a_split_replaces_collapsed_owner_with_stages_plus_extraction(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(workflow_update_module, "synthesize_code_block", lambda *a, **k: _fake_spine_synthesized())
        ctx = _spine_actuation_ctx()
        workflow_yaml = _collapsed_spine_yaml(
            _SPINE_SYNTH_CODE
            + '\nvalue = await page.locator("#result").inner_text()\nreturn {"output": {"record_id": value}}'
        )

        new_yaml, _metadata, applied = workflow_update_module._impose_output_contract_envelope_after_steering(
            ctx, workflow_yaml, []
        )

        assert applied is True
        blocks = workflow_blocks(parse_workflow_yaml(new_yaml))
        assert [block.get("label") for block in blocks] == [
            "extract_record_browser_stage_1",
            "extract_record_browser_stage_2",
            "extract_record",
        ]
        retained_code = str(blocks[-1].get("code") or "")
        assert "inner_text" in retained_code
        assert ".click()" not in retained_code

    def test_branch_a_result_is_idempotent_no_resplit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(workflow_update_module, "synthesize_code_block", lambda *a, **k: _fake_spine_synthesized())
        ctx = _spine_actuation_ctx()
        workflow_yaml = _collapsed_spine_yaml(
            _SPINE_SYNTH_CODE
            + '\nvalue = await page.locator("#result").inner_text()\nreturn {"output": {"record_id": value}}'
        )

        split_yaml, _meta, _applied = workflow_update_module._impose_output_contract_envelope_after_steering(
            ctx, workflow_yaml, []
        )
        again_yaml, _meta2, _applied2 = workflow_update_module._impose_output_contract_envelope_after_steering(
            ctx, split_yaml, []
        )

        assert [block.get("label") for block in workflow_blocks(parse_workflow_yaml(again_yaml))] == [
            "extract_record_browser_stage_1",
            "extract_record_browser_stage_2",
            "extract_record",
        ]
        assert not ctx.output_contract_spine_directive_blockers_by_attempt_key

    def test_branch_b_arms_directive_and_returns_unchanged_yaml(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(workflow_update_module, "synthesize_code_block", lambda *a, **k: _fake_spine_synthesized())
        ctx = _spine_actuation_ctx()
        workflow_yaml = _collapsed_spine_yaml(
            "_setup = 1\n" + _SPINE_SYNTH_CODE + '\nreturn {"output": {"record_id": "X"}}'
        )

        new_yaml, _metadata, applied = workflow_update_module._impose_output_contract_envelope_after_steering(
            ctx, workflow_yaml, []
        )

        assert applied is False
        assert new_yaml == workflow_yaml
        signature = workflow_update_module._stable_output_contract_key("turn:t-spine", {"output.record_id"})
        attempt_key = workflow_update_module._output_contract_spine_directive_attempt_key(
            signature=signature, block_label="extract_record", workflow_yaml=workflow_yaml
        )
        assert ctx.output_contract_spine_directive_blockers_by_attempt_key[attempt_key] == [
            "extraction_boundary_ambiguous"
        ]

    @pytest.mark.parametrize(
        "code_body, synth_kwargs, expected_blocker",
        [
            (
                "_setup = 1\n" + _SPINE_SYNTH_CODE + '\nreturn {"output": {"record_id": "X"}}',
                {},
                "extraction_boundary_ambiguous",
            ),
            (
                _SPINE_SYNTH_CODE + '\nawait page.locator("#extra").click()\nreturn {"output": {"record_id": "X"}}',
                {},
                "extraction_suffix_contains_browser_actions",
            ),
            (
                _SPINE_SYNTH_CODE + '\nvalue = await page.locator("#result").inner_text()',
                {},
                "static_return_envelope_unavailable",
            ),
            (
                _SPINE_SYNTH_CODE
                + '\nvalue = await page.locator("#result").inner_text()\nreturn {"output": {"record_id": value}}',
                {"steps": [{"line_start": 1, "line_end": 2}]},
                "insufficient_durable_stages",
            ),
            (
                _SPINE_SYNTH_CODE
                + '\nvalue = await page.locator("#result").inner_text()\nreturn {"output": {"record_id": value}}',
                {"parameters": [{"key": "alpha"}, {"key": "alpha"}]},
                "parameter_reconciliation_failed",
            ),
        ],
    )
    def test_branch_b_precondition_failures_arm_matching_blocker(
        self,
        monkeypatch: pytest.MonkeyPatch,
        code_body: str,
        synth_kwargs: dict[str, object],
        expected_blocker: str,
    ) -> None:
        monkeypatch.setattr(
            workflow_update_module, "synthesize_code_block", lambda *a, **k: _fake_spine_synthesized(**synth_kwargs)
        )
        ctx = _spine_actuation_ctx()
        workflow_yaml = _collapsed_spine_yaml(code_body)

        _new_yaml, _metadata, applied = workflow_update_module._impose_output_contract_envelope_after_steering(
            ctx, workflow_yaml, []
        )

        assert applied is False
        armed = list(ctx.output_contract_spine_directive_blockers_by_attempt_key.values())
        assert armed == [[expected_blocker]]

    def test_evaluation_enriches_repair_context_and_payload_after_directive_armed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(workflow_update_module, "synthesize_code_block", lambda *a, **k: _fake_spine_synthesized())
        ctx = _spine_actuation_ctx()
        workflow_yaml = _collapsed_spine_yaml(
            "_setup = 1\n" + _SPINE_SYNTH_CODE + '\nreturn {"output": {"record_id": "X"}}'
        )

        workflow_update_module._impose_output_contract_envelope_after_steering(ctx, workflow_yaml, [])
        evaluation = workflow_update_module._evaluate_output_contract_for_code_block(ctx, workflow_yaml, [])

        assert evaluation is not None
        assert evaluation.shape_violations == ["separated_spine_shape_required"]
        assert evaluation.repair_context is not None
        assert evaluation.repair_context.required_block_structure == "separated_browser_spine_plus_extraction"
        assert evaluation.repair_context.spine_split_blockers == ["extraction_boundary_ambiguous"]
        assert evaluation.payload["spine_structure_directive"]["spine_split_blockers"] == [
            "extraction_boundary_ambiguous"
        ]

    def test_directive_renders_into_next_authoring_prompt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(workflow_update_module, "synthesize_code_block", lambda *a, **k: _fake_spine_synthesized())
        ctx = _spine_actuation_ctx()
        workflow_yaml = _collapsed_spine_yaml(
            "_setup = 1\n" + _SPINE_SYNTH_CODE + '\nreturn {"output": {"record_id": "X"}}'
        )

        workflow_update_module._impose_output_contract_envelope_after_steering(ctx, workflow_yaml, [])
        evaluation = workflow_update_module._evaluate_output_contract_for_code_block(ctx, workflow_yaml, [])
        assert evaluation is not None
        ctx.last_code_authoring_repair_context = evaluation.repair_context

        rendered = agent_module._code_authoring_repair_context_prompt(ctx)

        assert "required_block_structure: separated_browser_spine_plus_extraction" in rendered
        assert "spine_split_blockers:" in rendered
        assert "one browser-stage code block per scouted mutation stage" in rendered

    def test_cosmetic_churn_escalates_to_advisory_instead_of_rearming(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(workflow_update_module, "synthesize_code_block", lambda *a, **k: _fake_spine_synthesized())
        ctx = _spine_actuation_ctx()
        first_yaml = _collapsed_spine_yaml(
            "_setup = 1\n" + _SPINE_SYNTH_CODE + '\nreturn {"output": {"record_id": "X"}}'
        )
        cosmetic_yaml = _collapsed_spine_yaml(
            "_setup = 2\n" + _SPINE_SYNTH_CODE + '\nreturn {"output": {"record_id": "X"}}'
        )

        workflow_update_module._impose_output_contract_envelope_after_steering(ctx, first_yaml, [])
        assert len(ctx.output_contract_spine_directive_blockers_by_attempt_key) == 1
        assert not ctx.output_contract_actuation_by_signature
        assert ctx.turn_halt is None

        workflow_update_module._impose_output_contract_envelope_after_steering(ctx, cosmetic_yaml, [])
        assert list(ctx.output_contract_actuation_by_signature.values()) == [OutputContractAdvisoryState.GRANTED]
        assert ctx.turn_halt is None
        assert len(ctx.output_contract_spine_directive_blockers_by_attempt_key) == 1


def _dual_output_owner_yaml() -> str:
    return _yaml(
        "title: Entry lookup\n"
        "workflow_definition:\n"
        "  blocks:\n"
        "  - block_type: code\n"
        "    label: extract_a\n"
        "    code: |\n"
        '      return {"output": {"record_id": "A"}}\n'
        "  - block_type: code\n"
        "    label: extract_b\n"
        "    code: |\n"
        '      return {"output": {"record_id": "B"}}\n'
    )


class TestAmbiguousOutputOwnerActuation:
    def _signature(self) -> str:
        return workflow_update_module._stable_output_contract_key("turn:t-spine", {"output.record_id"})

    def test_ambiguous_owner_arms_directive_instead_of_bailing(self) -> None:
        ctx = _spine_actuation_ctx()
        workflow_yaml = _dual_output_owner_yaml()

        new_yaml, _metadata, applied = workflow_update_module._impose_output_contract_envelope_after_steering(
            ctx, workflow_yaml, []
        )

        assert applied is False
        assert new_yaml == workflow_yaml
        assert ctx.output_contract_output_owner_directive_candidates_by_signature[self._signature()] == [
            "extract_a",
            "extract_b",
        ]

    def test_evaluation_enriches_owner_ambiguity_repair_context_after_directive_armed(self) -> None:
        ctx = _spine_actuation_ctx()
        workflow_yaml = _dual_output_owner_yaml()

        workflow_update_module._impose_output_contract_envelope_after_steering(ctx, workflow_yaml, [])
        evaluation = workflow_update_module._evaluate_output_contract_for_code_block(ctx, workflow_yaml, [])

        assert evaluation is not None
        assert "ambiguous_output_owner" in evaluation.shape_violations
        assert evaluation.repair_context is not None
        assert evaluation.repair_context.reason_code == "output_owner_ambiguous"
        assert evaluation.repair_context.output_owner_candidate_labels == ["extract_a", "extract_b"]
        assert evaluation.payload["output_owner_directive"]["output_owner_candidate_labels"] == [
            "extract_a",
            "extract_b",
        ]

    def test_owner_directive_renders_into_next_authoring_prompt(self) -> None:
        ctx = _spine_actuation_ctx()
        workflow_yaml = _dual_output_owner_yaml()

        workflow_update_module._impose_output_contract_envelope_after_steering(ctx, workflow_yaml, [])
        evaluation = workflow_update_module._evaluate_output_contract_for_code_block(ctx, workflow_yaml, [])
        assert evaluation is not None
        ctx.last_code_authoring_repair_context = evaluation.repair_context

        rendered = agent_module._code_authoring_repair_context_prompt(ctx)

        assert "output_owner_candidate_labels: extract_a, extract_b" in rendered
        assert "sole output owner" in rendered

    def test_owner_directive_emits_fingerprint_once_per_signature(self) -> None:
        ctx = _spine_actuation_ctx()
        workflow_yaml = _dual_output_owner_yaml()

        workflow_update_module._impose_output_contract_envelope_after_steering(ctx, workflow_yaml, [])
        workflow_update_module._impose_output_contract_envelope_after_steering(ctx, workflow_yaml, [])

        assert list(ctx.output_contract_output_owner_directive_candidates_by_signature.keys()) == [self._signature()]


def _already_split_spine_yaml(extra_sibling_code: str | None = None, *, base: str | None = None) -> str:
    base = base or workflow_update_module._SYNTHESIZED_BLOCK_LABEL
    extra_block = ""
    if extra_sibling_code is not None:
        extra_block = (
            f"  - block_type: code\n    label: {base}_browser_stage_extra\n    code: |\n"
            + textwrap.indent(textwrap.dedent(extra_sibling_code).strip(), " " * 6)
            + "\n"
        )
    return _yaml(
        "title: Entry lookup\n"
        "workflow_definition:\n"
        "  blocks:\n"
        f"  - block_type: code\n    label: {base}_browser_stage_1\n    code: |\n"
        '      await page.locator("#stage-a").click()\n'
        f"  - block_type: code\n    label: {base}_browser_stage_2\n    code: |\n"
        '      await page.locator("#stage-b").click()\n'
        f"{extra_block}"
        f"  - block_type: code\n    label: {base}\n    code: |\n"
        '      value = await page.locator("#result").inner_text()\n'
        '      return {"output": {"record_id": value}}\n'
    )


def _imposition_split_ctx() -> CopilotContext:
    ctx = _spine_actuation_ctx()
    ctx.impose_synthesized_code_block = True
    ctx.raw_code_artifact_metadata = [
        {
            "block_label": workflow_update_module._SYNTHESIZED_BLOCK_LABEL,
            "claimed_outcomes": [{"goal_value_paths": ["output.record_id"], "extraction_schema": '{"type":"object"}'}],
        }
    ]
    return ctx


class TestSeparatedSpineImpositionRunEligibility:
    def test_imposition_accepts_scouted_browser_stage_siblings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(workflow_update_module, "synthesize_code_block", lambda *a, **k: _fake_spine_synthesized())
        ctx = _imposition_split_ctx()

        result = workflow_update_module._maybe_impose_synthesized_code_block(_already_split_spine_yaml(), ctx)

        assert result.violations == []

    def test_imposition_still_flags_unscouted_sibling_mutation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(workflow_update_module, "synthesize_code_block", lambda *a, **k: _fake_spine_synthesized())
        ctx = _imposition_split_ctx()

        result = workflow_update_module._maybe_impose_synthesized_code_block(
            _already_split_spine_yaml(extra_sibling_code='await page.locator("#hallucinated").click()'), ctx
        )

        assert any("unscouted browser action" in violation for violation in result.violations)
        assert any("#hallucinated" in violation for violation in result.violations)
        flagged_actions = [violation.split(" Provenance: ")[0] for violation in result.violations]
        assert not any("#stage-a" in flagged for flagged in flagged_actions)

    def test_whole_trajectory_validation_exempts_spine_covered_sibling_mutations(self) -> None:
        parsed = parse_workflow_yaml(_already_split_spine_yaml())
        blocks = workflow_update_module._workflow_code_blocks(parsed)
        extraction_block = next(
            block for block in blocks if block.get("label") == workflow_update_module._SYNTHESIZED_BLOCK_LABEL
        )

        validation = workflow_update_module._whole_trajectory_browser_surface_violations(
            code_blocks=blocks,
            selected_code_block=extraction_block,
            submitted_selected_code=str(extraction_block.get("code") or ""),
            synthesized_code=_SPINE_SYNTH_CODE,
        )

        assert validation.violations == []

    def test_armed_attempt_key_survives_scaffold_metadata_owner_shift(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(workflow_update_module, "synthesize_code_block", lambda *a, **k: _fake_spine_synthesized())
        ctx = _spine_actuation_ctx()
        workflow_yaml = _collapsed_spine_yaml(
            "_setup = 1\n" + _SPINE_SYNTH_CODE + '\nreturn {"output": {"record_id": "X"}}'
        )

        workflow_update_module._impose_output_contract_envelope_after_steering(ctx, workflow_yaml, [])
        armed_keys = set(ctx.output_contract_spine_directive_blockers_by_attempt_key)
        assert len(armed_keys) == 1

        scaffolded_metadata, _applied = workflow_update_module._scaffold_metadata_contract_for_update(
            ctx, workflow_yaml, []
        )
        contract = workflow_update_module._output_contract_required_paths_source(ctx)
        required_paths = contract.union
        read_label, _owner_labels = workflow_update_module._target_output_contract_block_label(
            ctx, workflow_yaml, scaffolded_metadata, required_paths
        )
        read_signature = workflow_update_module._output_contract_signature(ctx=ctx, required_paths=required_paths)
        read_key = workflow_update_module._output_contract_spine_directive_attempt_key(
            signature=read_signature, block_label=read_label, workflow_yaml=workflow_yaml
        )

        assert read_key in armed_keys

    def test_granted_structural_advisory_lifts_output_contract_run_gate_on_unsplit_draft(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(workflow_update_module, "synthesize_code_block", lambda *a, **k: _fake_spine_synthesized())
        ctx = _spine_actuation_ctx()
        collapsed_yaml = _collapsed_spine_yaml(
            "_setup = 1\n" + _SPINE_SYNTH_CODE + '\nreturn {"output": {"record_id": "X"}}'
        )

        workflow_update_module._impose_output_contract_envelope_after_steering(ctx, collapsed_yaml, [])
        blocked = workflow_update_module._evaluate_output_contract_for_code_block(
            ctx, collapsed_yaml, [], allow_static_return_advisory=True
        )
        assert blocked is not None and blocked.can_attempt_run is False

        workflow_update_module._impose_output_contract_envelope_after_steering(ctx, collapsed_yaml, [])
        assert list(ctx.output_contract_actuation_by_signature.values()) == [OutputContractAdvisoryState.GRANTED]

        scaffolded_metadata, _applied = workflow_update_module._scaffold_metadata_contract_for_update(
            ctx, collapsed_yaml, []
        )
        granted = workflow_update_module._evaluate_output_contract_for_code_block(
            ctx, collapsed_yaml, scaffolded_metadata, allow_static_return_advisory=True
        )
        assert granted is not None and granted.can_attempt_run is True


def _budget_evaluation(ctx: CopilotContext) -> _OutputContractEvaluation:
    workflow_yaml = _collapsed_spine_yaml(
        _SPINE_SYNTH_CODE
        + '\nvalue = await page.locator("#result").inner_text()\nreturn {"output": {"record_id": value}}'
    )
    evaluation = workflow_update_module._evaluate_output_contract_for_code_block(ctx, workflow_yaml, [])
    assert evaluation is not None
    return evaluation


class TestCodeBlockParameterPersistSeam:
    def test_declared_credential_key_is_adopted_for_unresolved_name(self) -> None:
        workflow_yaml = _yaml(
            """
            title: Portal login
            workflow_definition:
              parameters:
              - parameter_type: workflow
                workflow_parameter_type: credential_id
                key: portal_credentials
                default_value: cred_123
              blocks:
              - block_type: code
                label: sign_in
                code: |
                  await page.locator("#user").fill(portal_credentials.username)
            """
        )

        adopted = workflow_update_module._adopt_exact_declared_parameter_keys_for_unresolved_names(workflow_yaml)

        parsed = parse_workflow_yaml(adopted)
        blocks = workflow_blocks(parsed)
        assert blocks[0]["parameter_keys"] == ["portal_credentials"]

    def test_output_contract_graph_preserves_branch_isolation(self) -> None:
        workflow_yaml = _yaml(
            """
            workflow_definition:
              blocks:
              - block_type: code
                label: start_search
                code: |
                  return {"ok": True}
              - block_type: task
                label: choose_path
                branch_conditions:
                - blocks:
                  - block_type: code
                    label: branch_a
                    code: |
                      return start_search_output
                - blocks:
                  - block_type: code
                    label: branch_b
                    code: |
                      return start_search_output
              - block_type: code
                label: after_choice
                code: |
                  return choose_path_output
            """
        )

        available_by_label = code_block_available_binding_keys_by_label(workflow_yaml)

        assert available_by_label["branch_a"] == ["start_search_output"]
        assert available_by_label["branch_b"] == ["start_search_output"]
        assert "branch_a_output" not in available_by_label["branch_b"]
        assert available_by_label["after_choice"] == ["choose_path_output", "start_search_output"]

    @pytest.mark.asyncio
    async def test_declared_workflow_string_parameter_key_is_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        submitted = _code_yaml(
            "await page.locator('#query').fill(str(provider_query))",
            parameter_keys=["provider_query"],
            workflow_param=True,
        )

        result = await _update_workflow({"workflow_yaml": submitted}, ctx)

        assert result["ok"] is True
        assert ctx.workflow_yaml == submitted

    @pytest.mark.asyncio
    async def test_new_declared_workflow_parameter_key_is_accepted_on_later_edit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        ctx.workflow_yaml = _code_yaml(
            "await page.locator('#query').fill(str(provider_query))",
            parameter_keys=["provider_query"],
            workflow_param=True,
        )
        submitted = _yaml(
            """
            title: Registry lookup
            workflow_definition:
              parameters:
              - {parameter_type: workflow, workflow_parameter_type: string, key: provider_query, default_value: Sample Search}
              - {parameter_type: workflow, workflow_parameter_type: string, key: search_location, default_value: Example City}
              blocks:
              - block_type: code
                label: search_registry
                parameter_keys: [provider_query, search_location]
                code: |
                  await page.locator("#query").fill(str(provider_query))
                  await page.locator("#location").fill(str(search_location))
            """
        )

        result = await _update_workflow({"workflow_yaml": submitted}, ctx)

        assert result["ok"] is True
        assert ctx.workflow_yaml == submitted

    @pytest.mark.asyncio
    async def test_prior_block_output_parameter_key_is_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        submitted = _directory_blocks_yaml(
            """
            - block_type: code
              label: search_registry
              code: |
                return {"records": []}
            - block_type: code
              label: summarize_registry
              parameter_keys: [search_registry_output]
              code: |
                print(search_registry_output)
            """
        )

        result = await _update_workflow({"workflow_yaml": submitted}, ctx)

        assert result["ok"] is True
        assert ctx.workflow_yaml == submitted

    @pytest.mark.parametrize("marker", ["<<<<<<< HEAD", "======="])
    @pytest.mark.asyncio
    async def test_raw_workflow_yaml_conflict_marker_rejects_before_persist(
        self, monkeypatch: pytest.MonkeyPatch, marker: str
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        ctx.workflow_yaml = _SAFE_CODE_YAML
        submitted = f"{marker}\n{_SAFE_CODE_YAML}"

        result = await _update_workflow({"workflow_yaml": submitted}, ctx)

        assert result["ok"] is False
        assert f"conflict marker `{marker}`" in result["error"]
        assert "line 1" in result["error"]
        assert ctx.workflow_yaml == _SAFE_CODE_YAML


class TestCompiledAuthoringImposition:
    def _provider_search_ctx(self) -> CopilotContext:
        ctx = _code_only_ctx()
        _enable_imposition(ctx)
        ctx.scout_trajectory = [
            {
                "tool_name": "type_text",
                "selector": "#provInput",
                "source_url": "https://example.com/find-care",
                "typed_length": 13,
                "role": "textbox",
                "accessible_name": "Provider Name",
                "trajectory_index": 0,
            }
        ]
        return ctx

    def _typed_default_ctx(self) -> CopilotContext:
        ctx = _code_only_ctx()
        _enable_imposition(ctx)
        ctx.scout_trajectory = [
            {
                "tool_name": "type_text",
                "selector": "#search",
                "source_url": "https://example.com/catalog",
                "typed_length": 15,
                "typed_value": "example_sku_123",
                "role": "textbox",
                "accessible_name": "Search",
                "trajectory_index": 0,
            }
        ]
        return ctx

    @staticmethod
    def _cafe_search_capture() -> dict[str, object]:
        return {
            "tool_name": "type_text",
            "selector": "#café-search",
            "source_url": "https://example.com/catalog",
            "typed_length": 15,
            "role": "textbox",
            "accessible_name": "Catalog search",
            "trajectory_index": 0,
        }

    @pytest.mark.asyncio
    async def test_imposes_strict_scout_selector_and_lifts_singleton_literal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = self._provider_search_ctx()

        result = await _update_workflow({"workflow_yaml": _SUBMITTED_LITERAL_YAML}, ctx)

        assert result["ok"] is True
        parsed = parse_workflow_yaml(ctx.workflow_yaml)
        assert isinstance(parsed, dict)
        block = _single_code_block(parsed)
        assert 'await page.locator("#provInput").fill(str(provider_name))' in block["code"]
        assert "input[placeholder='Search']" not in block["code"]
        assert block["parameter_keys"] == ["provider_name"]
        parameters = parsed["workflow_definition"]["parameters"]
        assert parameters == [
            {
                "parameter_type": "workflow",
                "workflow_parameter_type": "string",
                "key": "provider_name",
                "default_value": "Sample Search",
            }
        ]
        assert result["data"]["imposed_substitutions"] == {
            "block_label": "search_registry",
            "source_trajectory_count": 1,
            "parameter_keys": ["provider_name"],
            "credential_parameter_keys": [],
            "selector_provenance": [
                {
                    "trajectory_index": 0,
                    "selector": "#provInput",
                    "emitted_literal": "#provInput",
                    "source": "selector",
                }
            ],
            "prior_source": "workflow_yaml",
        }

    @pytest.mark.asyncio
    async def test_enter_directory_location_output_block_metadata_passes_before_persist(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        submitted = _yaml(
            """
            title: Directory lookup
            workflow_definition:
              blocks:
              - block_type: code
                label: enter_directory_location
                prompt: Enter the directory location and output the selected address.
                code: |
                  await page.locator("#locInput").fill("North Carolina, USA")
                  address = "North Carolina, USA"
            """
        )
        metadata = [
            {
                "block_label": "enter_directory_location",
                "declared_goal": "Enter the directory location and return the address.",
                "claimed_outcomes": [
                    {
                        "id": "claim:address",
                        "status": "observed_not_verified",
                        "goal_value_paths": ["address"],
                    }
                ],
                "terminal_verifier_expectations": [
                    {
                        "id": "expectation:address",
                        "goal_value_paths": ["address"],
                    }
                ],
            }
        ]

        result = await _update_workflow({"workflow_yaml": submitted, "code_artifact_metadata": metadata}, ctx)

        assert result["ok"] is True
        assert ctx.workflow_yaml
        assert "enter_directory_location" in ctx.code_artifact_metadata
        assert ctx.latest_recorded_build_test_outcome is None

    @pytest.mark.asyncio
    async def test_imposition_preserves_submitted_extraction_suffix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_successful_update(monkeypatch)
        ctx = self._provider_search_ctx()
        synthesized = workflow_update_module.synthesize_code_block(ctx.scout_trajectory, strict_selectors=True)
        assert synthesized is not None
        submitted_code = synthesized.code.rstrip() + '\nrecords = [{"number": "REC-001", "status": "credentialed"}]\n'
        submitted = yaml.safe_dump(
            {
                "title": "Provider lookup",
                "workflow_definition": {
                    "parameters": [
                        {
                            "parameter_type": "workflow",
                            "workflow_parameter_type": "string",
                            "key": "provider_name",
                            "default_value": "Sample Search",
                        }
                    ],
                    "blocks": [
                        {
                            "block_type": "code",
                            "label": "search_registry",
                            "code": submitted_code,
                        }
                    ],
                },
            },
            sort_keys=False,
        )
        metadata = [_terminal_metadata("search_registry", "search the registry")]

        result = await _update_workflow({"workflow_yaml": submitted, "code_artifact_metadata": metadata}, ctx)

        assert result["ok"] is True
        assert result["data"]["imposed_substitutions"]["preserved_extraction_suffix"] is True
        parsed = parse_workflow_yaml(ctx.workflow_yaml)
        assert isinstance(parsed, dict)
        code_blocks = [block for block in workflow_blocks(parsed) if block.get("block_type") == "code"]
        assert len(code_blocks) > 1
        output_block = _code_blocks(parsed)["search_registry"]
        browser_code = "\n".join(str(block.get("code") or "") for block in code_blocks[:-1])
        output_code = str(output_block["code"])
        assert "<fill" not in browser_code
        assert "<fill: captured value>" not in browser_code
        assert 'await page.locator("#provInput").fill(str(provider_name))' in browser_code
        assert 'records = [{"number": "REC-001", "status": "credentialed"}]' in output_code
        assert 'return {"records": records}' in output_code

    @pytest.mark.asyncio
    async def test_a_returning_draft_the_surface_scan_cannot_partition_is_kept_over_the_spine(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A live draft named its locators, which the surface scan reports as ambiguous, so neither
        # preservation path applied and the spine replaced an extraction the run needed to register.
        _stub_successful_update(monkeypatch)
        ctx = self._provider_search_ctx()
        submitted_code = (
            'field = page.locator("#provInput")\n'
            "await field.fill(str(provider_name))\n"
            'value = await page.inner_text("#rec")\n'
            'return {"records": [{"number": value}]}\n'
        )
        _mutations, _unscouted, ambiguous = workflow_update_module._browser_surface_for_code(submitted_code)
        assert ambiguous, "fixture must reproduce the unpartitionable shape"

        submitted = yaml.safe_dump(
            {
                "title": "Provider lookup",
                "workflow_definition": {
                    "parameters": [
                        {
                            "parameter_type": "workflow",
                            "workflow_parameter_type": "string",
                            "key": "provider_name",
                            "default_value": "Sample Search",
                        }
                    ],
                    "blocks": [{"block_type": "code", "label": "search_registry", "code": submitted_code}],
                },
            },
            sort_keys=False,
        )
        metadata = [_terminal_metadata("search_registry", "search the registry")]

        result = await _update_workflow({"workflow_yaml": submitted, "code_artifact_metadata": metadata}, ctx)

        assert result["ok"] is True
        parsed = parse_workflow_yaml(ctx.workflow_yaml)
        assert isinstance(parsed, dict)
        persisted = "\n".join(
            str(block.get("code") or "") for block in workflow_blocks(parsed) if block.get("block_type") == "code"
        )
        assert 'return {"records": [{"number": value}]}' in persisted
        assert 'await page.inner_text("#rec")' in persisted

    @pytest.mark.asyncio
    async def test_a_read_only_extraction_survives_metadata_that_declares_no_goal_values(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = self._provider_search_ctx()
        synthesized = workflow_update_module.synthesize_code_block(ctx.scout_trajectory, strict_selectors=True)
        assert synthesized is not None
        extraction = 'records = [{"number": await page.inner_text("#rec")}]\nreturn {"records": records}\n'
        submitted = yaml.safe_dump(
            {
                "title": "Provider lookup",
                "workflow_definition": {
                    "parameters": [
                        {
                            "parameter_type": "workflow",
                            "workflow_parameter_type": "string",
                            "key": "provider_name",
                            "default_value": "Sample Search",
                        }
                    ],
                    "blocks": [{"block_type": "code", "label": "search_registry", "code": extraction}],
                },
            },
            sort_keys=False,
        )

        result = await _update_workflow({"workflow_yaml": submitted}, ctx)

        assert result["ok"] is True
        persisted = "\n".join(
            str(block.get("code") or "") for block in _code_blocks(parse_workflow_yaml(ctx.workflow_yaml)).values()
        )
        assert "#provInput" in persisted, "the demonstrated spine must survive"
        assert "#rec" in persisted, "the model's extraction must survive"

    def _login_otp_ctx(self) -> CopilotContext:
        login_url = "https://example.com/login"
        ctx = _code_only_ctx()
        _enable_imposition(ctx)
        ctx.scout_trajectory = [
            _credential_fill_interaction("username", credential_id="cred_1", source_url=login_url),
            _credential_fill_interaction("password", credential_id="cred_1", source_url=login_url),
            {
                "tool_name": "click",
                "selector": ".btn-login",
                "source_url": login_url,
                "role": "button",
                "accessible_name": "Log in",
            },
            {
                "tool_name": "press_key",
                "selector": "input[type='password']",
                "key": "Enter",
                "source_url": login_url,
            },
            {
                "tool_name": "read_value",
                "read_expression": 'document.querySelector("#otp-hint").innerText',
                "read_output_path": "output.otp_hint",
                "source_url": login_url,
            },
            _credential_fill_interaction("totp", credential_id="cred_1", source_url=login_url),
            {
                "tool_name": "click",
                "selector": ".btn-primary-submit",
                "source_url": login_url,
                "role": "button",
                "accessible_name": "Login",
            },
        ]
        return ctx

    _INVENTED_SUBMIT_NAME_YAML = _credential_code_yaml(
        credential_id="cred_1",
        code="""
        await page.locator("#email").fill(login_credential.username)
        await page.locator("input[type='password']").fill(login_credential.password)
        await page.locator(".btn-login").click()
        await page.locator("#totpmfa").fill(await login_credential.otp())
        await page.get_by_role("button", name="Continue", exact=True).click()
        """,
    )

    @pytest.mark.asyncio
    async def test_login_otp_persist_imposes_demonstrated_submit_over_invented_role_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = self._login_otp_ctx()

        result = await _update_workflow(
            {"workflow_yaml": self._INVENTED_SUBMIT_NAME_YAML}, ctx, allow_missing_credentials=True
        )

        assert result["ok"] is True
        assert result["data"]["imposed_substitutions"] is not None
        parsed = parse_workflow_yaml(ctx.workflow_yaml)
        assert isinstance(parsed, dict)
        code = str(_single_code_block(parsed)["code"])
        assert 'await page.locator(".btn-primary-submit").click()' in code
        assert "Continue" not in code

    @pytest.mark.asyncio
    async def test_login_otp_persist_retains_the_captured_output_read(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_successful_update(monkeypatch)
        ctx = self._login_otp_ctx()

        result = await _update_workflow(
            {"workflow_yaml": self._INVENTED_SUBMIT_NAME_YAML}, ctx, allow_missing_credentials=True
        )

        assert result["ok"] is True
        parsed = parse_workflow_yaml(ctx.workflow_yaml)
        assert isinstance(parsed, dict)
        code = str(_single_code_block(parsed)["code"])
        assert "await page.evaluate('document.querySelector(\"#otp-hint\").innerText')" in code

    def _download_ctx(self) -> CopilotContext:
        from skyvern.forge.sdk.copilot.reached_download_target import ReachedDownloadTarget

        ctx = _code_only_ctx()
        _enable_imposition(ctx)
        ctx.scout_trajectory = [
            {
                "tool_name": "click",
                "selector": "#statement-row",
                "source_url": "https://example.com/billing",
                "trajectory_index": 0,
            }
        ]
        ctx.reached_download_target = ReachedDownloadTarget(
            selector='a[href="/billing/statement.pdf"]',
            affordance_text="View Printable Statement",
            download_kind="attribute",
            source_step="trajectory_recency",
            already_registered=False,
        )
        return ctx

    @pytest.mark.asyncio
    async def test_imposition_forwards_reached_target_and_emits_download_terminal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = self._download_ctx()
        submitted = _yaml(
            """
            title: Statement download
            workflow_definition:
              blocks:
              - block_type: code
                label: download_statement
                code: |
                  await page.locator("#statement-row").click()
            """
        )

        result = await _update_workflow({"workflow_yaml": submitted}, ctx)

        assert result["ok"] is True
        parsed = parse_workflow_yaml(ctx.workflow_yaml)
        assert isinstance(parsed, dict)
        block = _single_code_block(parsed)
        assert "async with page.expect_download()" in block["code"]
        assert "/billing/statement.pdf" in block["code"]
        assert "save_as" not in block["code"]
        assert result["data"]["imposed_substitutions"]["block_label"] == "download_statement"

    @pytest.mark.asyncio
    async def test_imposed_download_terminal_clears_binding_gate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_successful_update(monkeypatch)
        ctx = self._download_ctx()
        ctx.flow_evidence = [
            {
                "evidence": {"source_tool": "scout_interaction", "interaction_selector": "#statement-row"},
                "reached_via": "interaction",
            }
        ]
        submitted = _yaml(
            """
            title: Statement download
            workflow_definition:
              blocks:
              - block_type: code
                label: download_statement
                code: |
                  await page.locator("#statement-row").click()
            """
        )

        result = await _update_workflow({"workflow_yaml": submitted}, ctx)

        assert result["ok"] is True
        block = _single_code_block(parse_workflow_yaml(ctx.workflow_yaml))
        assert "async with page.expect_download()" in block["code"]

    @pytest.mark.asyncio
    async def test_reached_download_target_still_imposes_after_prior_update_attempt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = self._download_ctx()
        ctx.update_workflow_called = True
        submitted = _yaml(
            """
            title: Statement download
            workflow_definition:
              blocks:
              - block_type: code
                label: download_statement
                code: |
                  await page.locator("#statement-row").click()
            """
        )

        result = await _update_workflow({"workflow_yaml": submitted}, ctx)

        assert result["ok"] is True
        block = _single_code_block(parse_workflow_yaml(ctx.workflow_yaml))
        assert "async with page.expect_download()" in block["code"]

    def test_imposition_targets_synthesized_label_in_multi_code_workflow(self) -> None:
        ctx = self._download_ctx()
        workflow_yaml = _yaml(
            """
            title: Statement download
            workflow_definition:
              blocks:
              - block_type: code
                label: summarize_statement
                code: |
                  return {"status": "ready"}
              - block_type: code
                label: scout_synthesized_browser_steps
                code: |
                  await page.locator("#statement-row").click()
            """
        )

        result = workflow_update_module._maybe_impose_synthesized_code_block(workflow_yaml, ctx)

        assert result.violations == []
        parsed = parse_workflow_yaml(result.workflow_yaml)
        assert isinstance(parsed, dict)
        blocks = {str(block.get("label")): block for block in workflow_blocks(parsed)}
        assert blocks["summarize_statement"]["code"].strip() == 'return {"status": "ready"}'
        assert "async with page.expect_download()" in blocks["scout_synthesized_browser_steps"]["code"]

    def test_imposition_targets_recorded_outcome_owner_in_multi_code_workflow(self) -> None:
        ctx = self._download_ctx()
        ctx.recorded_outcome_binding_constraint = RecordedOutcomeBindingConstraint(
            repeated_structural_key="recorded-download",
            phase="persisted_block_run",
            reason_code="outcome_not_demonstrated",
            frontier_facet="value_shape",
            owning_block_labels=["download_matching_invoice_pdf"],
        )
        workflow_yaml = _yaml(
            """
            title: Statement download
            workflow_definition:
              blocks:
              - block_type: code
                label: login_to_apex_business
                code: |
                  return {"logged_in": True}
              - block_type: code
                label: download_matching_invoice_pdf
                code: |
                  async with page.expect_download() as download_info:
                      await page.locator("a[href='/billing/statement.pdf']").click()
                  download = await download_info.value
                  return {"downloaded_files": [download.suggested_filename]}
            """
        )

        result = workflow_update_module._maybe_impose_synthesized_code_block(workflow_yaml, ctx)

        assert result.violations == []
        assert result.substitutions is not None
        parsed = parse_workflow_yaml(result.workflow_yaml)
        assert isinstance(parsed, dict)
        blocks = {str(block.get("label")): block for block in workflow_blocks(parsed)}
        assert blocks["login_to_apex_business"]["code"].strip() == 'return {"logged_in": True}'
        assert "async with page.expect_download()" in blocks["download_matching_invoice_pdf"]["code"]

    def test_imposition_ignores_unchanged_persisted_browser_sibling(self) -> None:
        ctx = self._download_ctx()
        prior_yaml = _yaml(
            """
            title: Statement download
            workflow_definition:
              blocks:
              - block_type: code
                label: login_to_apex_business
                code: |
                  await page.locator("#email").fill(str(login_credentials["username"]))
                  await page.locator("#password").fill(str(login_credentials["password"]))
                  await page.locator("#sign-in").click()
              - block_type: code
                label: download_matching_invoice_pdf
                code: |
                  return {"status": "ready"}
            """
        )
        submitted_yaml = _yaml(
            """
            title: Statement download
            workflow_definition:
              blocks:
              - block_type: code
                label: login_to_apex_business
                code: |
                  await page.locator("#email").fill(str(login_credentials["username"]))
                  await page.locator("#password").fill(str(login_credentials["password"]))
                  await page.locator("#sign-in").click()
              - block_type: code
                label: download_matching_invoice_pdf
                code: |
                  await page.locator("#statement-row").click()
            """
        )
        ctx.workflow_yaml = prior_yaml

        result = workflow_update_module._maybe_impose_synthesized_code_block(submitted_yaml, ctx)

        assert result.violations == []
        assert result.substitutions is not None
        parsed = parse_workflow_yaml(result.workflow_yaml)
        assert isinstance(parsed, dict)
        blocks = {str(block.get("label")): block for block in workflow_blocks(parsed)}
        assert "#sign-in" in str(blocks["login_to_apex_business"]["code"])
        assert "async with page.expect_download()" in str(blocks["download_matching_invoice_pdf"]["code"])

    def test_prior_synthesized_label_prevents_unrelated_multi_code_imposition(self) -> None:
        ctx = self._download_ctx()
        prior_yaml = _yaml(
            """
            title: Statement download
            workflow_definition:
              blocks:
              - block_type: code
                label: summarize_statement
                code: |
                  return {"status": "ready"}
              - block_type: code
                label: scout_synthesized_browser_steps
                code: |
                  await page.locator("#statement-row").click()
            """
        )
        submitted_yaml = _yaml(
            """
            title: Statement download
            workflow_definition:
              blocks:
              - block_type: code
                label: summarize_statement
                code: |
                  return {"status": "edited"}
              - block_type: code
                label: scout_synthesized_browser_steps
                code: |
                  await page.locator("#statement-row").click()
            """
        )
        ctx.workflow_yaml = prior_yaml

        result = workflow_update_module._maybe_impose_synthesized_code_block(submitted_yaml, ctx)

        assert result.violations == []
        assert result.substitutions is None
        parsed = parse_workflow_yaml(result.workflow_yaml)
        assert isinstance(parsed, dict)
        blocks = {str(block.get("label")): block for block in workflow_blocks(parsed)}
        assert blocks["summarize_statement"]["code"].strip() == 'return {"status": "edited"}'
        assert blocks["scout_synthesized_browser_steps"]["code"].strip() == (
            'await page.locator("#statement-row").click()'
        )

    @pytest.mark.asyncio
    async def test_unchanged_prior_code_does_not_impose(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_successful_update(monkeypatch)
        ctx = self._provider_search_ctx()
        ctx.scout_trajectory = [
            {
                "tool_name": "type_text",
                "selector": "input[placeholder='Search']",
                "source_url": "https://example.com/find-care",
                "typed_length": 13,
                "role": "textbox",
                "accessible_name": "Search",
                "trajectory_index": 0,
            },
            {
                "tool_name": "click",
                "selector": "button.lookup",
                "source_url": "https://example.com/find-care",
                "role": "button",
                "accessible_name": "Lookup",
                "trajectory_index": 1,
            },
        ]
        ctx.workflow_yaml = _SUBMITTED_LITERAL_YAML

        result = await _update_workflow({"workflow_yaml": _SUBMITTED_LITERAL_YAML}, ctx)

        assert result["ok"] is True
        assert "imposed_substitutions" not in result["data"]
        assert ctx.workflow_yaml == _SUBMITTED_LITERAL_YAML

    @pytest.mark.asyncio
    async def test_flag_off_code_only_mode_does_not_impose(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_successful_update(monkeypatch)
        ctx = self._provider_search_ctx()
        ctx.impose_synthesized_code_block = False

        result = await _update_workflow({"workflow_yaml": _SUBMITTED_LITERAL_YAML}, ctx)

        assert result["ok"] is True
        assert "imposed_substitutions" not in result["data"]
        assert ctx.workflow_yaml == _SUBMITTED_LITERAL_YAML

    @pytest.mark.asyncio
    async def test_flag_off_code_only_mode_does_not_promote_scouted_typed_defaults(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = self._typed_default_ctx()
        ctx.impose_synthesized_code_block = False

        result = await _update_workflow({"workflow_yaml": _SUBMITTED_TYPED_LITERAL_REWRITE_YAML}, ctx)

        assert result["ok"] is True
        assert ctx.workflow_yaml == _SUBMITTED_TYPED_LITERAL_REWRITE_YAML

    @pytest.mark.asyncio
    async def test_single_direct_synthesized_parameter_reference_creates_required_input(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = self._provider_search_ctx()

        result = await _update_workflow({"workflow_yaml": _SUBMITTED_COMPUTED_LITERAL_YAML}, ctx)

        assert result["ok"] is True
        parsed = parse_workflow_yaml(ctx.workflow_yaml)
        assert isinstance(parsed, dict)
        block = _single_code_block(parsed)
        assert block["parameter_keys"] == ["provider_name"]
        assert parsed["workflow_definition"]["parameters"] == [
            {
                "parameter_type": "workflow",
                "workflow_parameter_type": "string",
                "key": "provider_name",
            }
        ]
        expected_selector = ctx.scout_trajectory[0]["selector"]
        assert f'await page.locator("{expected_selector}").fill(str(provider_name))' in block["code"]

    @pytest.mark.asyncio
    async def test_repeated_direct_synthesized_parameter_reference_creates_required_input(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = self._provider_search_ctx()

        result = await _update_workflow({"workflow_yaml": _SUBMITTED_REPEATED_COMPUTED_LITERAL_YAML}, ctx)

        assert result["ok"] is True
        parsed = parse_workflow_yaml(ctx.workflow_yaml)
        assert isinstance(parsed, dict)
        block = _single_code_block(parsed)
        assert block["parameter_keys"] == ["provider_name"]
        assert parsed["workflow_definition"]["parameters"] == [
            {
                "parameter_type": "workflow",
                "workflow_parameter_type": "string",
                "key": "provider_name",
            }
        ]
        assert 'await page.locator("#provInput").fill(str(provider_name))' in block["code"]

    @pytest.mark.asyncio
    async def test_mixed_non_locator_fill_does_not_hide_direct_synthesized_parameter_reference(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = self._provider_search_ctx()

        result = await _update_workflow({"workflow_yaml": _SUBMITTED_MIXED_FILL_COMPUTED_LITERAL_YAML}, ctx)

        assert result["ok"] is True
        parsed = parse_workflow_yaml(ctx.workflow_yaml)
        assert isinstance(parsed, dict)
        block = _single_code_block(parsed)
        assert block["parameter_keys"] == ["provider_name"]
        expected_selector = ctx.scout_trajectory[0]["selector"]
        assert f'await page.locator("{expected_selector}").fill(str(provider_name))' in block["code"]

    @pytest.mark.asyncio
    async def test_selector_join_aliases_synthesized_parameter_to_authored_fill_parameter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = self._provider_search_ctx()
        submitted = _yaml(
            """
            title: Provider lookup
            workflow_definition:
              parameters:
              - parameter_type: workflow
                workflow_parameter_type: string
                key: search_term
              blocks:
              - block_type: code
                label: search_registry
                code: |
                  await page.locator("#provInput").fill(search_term)
            """
        )

        result = await _update_workflow({"workflow_yaml": submitted}, ctx)

        assert result["ok"] is True
        parsed = parse_workflow_yaml(ctx.workflow_yaml)
        assert isinstance(parsed, dict)
        block = _single_code_block(parsed)
        assert block["parameter_keys"] == ["search_term"]
        assert 'await page.locator("#provInput").fill(str(search_term))' in block["code"]
        assert "provider_name" not in block["code"]

    @pytest.mark.asyncio
    async def test_synthesized_internal_parameter_aliases_to_existing_declared_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        _enable_imposition(ctx)
        ctx.scout_trajectory = [
            {
                "tool_name": "type_text",
                "selector": "#locInput",
                "source_url": "https://example.com/find-care",
                "typed_length": 17,
                "typed_value": "Example City, USA",
                "role": "textbox",
                "accessible_name": "Address or postal code",
                "trajectory_index": 0,
            }
        ]
        submitted = _yaml(
            """
            title: Directory lookup
            workflow_definition:
              parameters:
              - {key: search_location, default_value: "Example City, USA"}
              - {key: address_or_postal_code, default_value: "Example City, USA"}
              blocks:
              - block_type: code
                label: search_directory
                parameter_keys: [address_or_postal_code]
                parameters:
                - {key: address_or_postal_code, default_value: "Example City, USA"}
                code: |
                  await page.locator("#location").fill(str(address_or_postal_code))
            """
        )

        result = await _update_workflow({"workflow_yaml": submitted}, ctx)

        assert result["ok"] is True
        parsed = parse_workflow_yaml(ctx.workflow_yaml)
        assert isinstance(parsed, dict)
        block = _single_code_block(parsed)
        assert block["parameter_keys"] == ["search_location"]
        assert "parameters" not in block
        assert "str(search_location)" in block["code"]
        assert "address_or_postal_code" not in block["code"]
        parameters = parsed["workflow_definition"]["parameters"]
        assert [parameter["key"] for parameter in parameters] == ["search_location"]
        assert result["data"]["imposed_substitutions"]["parameter_aliases"] == {
            "address_or_postal_code": "search_location"
        }

    @pytest.mark.asyncio
    async def test_multi_input_synthesized_parameter_aliases_before_ambiguity_guard(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        _enable_imposition(ctx)
        ctx.scout_trajectory = [
            {
                "tool_name": "type_text",
                "selector": "#locInput",
                "source_url": "https://example.com/find-care",
                "typed_length": 17,
                "typed_value": "Example City, USA",
                "role": "textbox",
                "accessible_name": "Address or postal code",
                "trajectory_index": 0,
            },
            {
                "tool_name": "type_text",
                "selector": "#firstName",
                "source_url": "https://example.com/find-care",
                "typed_length": 5,
                "typed_value": "Given",
                "role": "textbox",
                "accessible_name": "Provider First Name",
                "trajectory_index": 1,
            },
        ]
        submitted = _yaml(
            """
            title: Directory lookup
            workflow_definition:
              parameters:
              - {key: search_location, default_value: "Example City, USA"}
              - {key: provider_first_name, default_value: "Given"}
              blocks:
              - block_type: code
                label: search_directory
                parameter_keys: [address_or_postal_code, provider_first_name]
                code: |
                  await page.locator("#location").fill(str(address_or_postal_code))
                  await page.locator("#firstName").fill(str(provider_first_name))
            """
        )

        result = await _update_workflow({"workflow_yaml": submitted}, ctx)

        assert result["ok"] is True
        parsed = parse_workflow_yaml(ctx.workflow_yaml)
        assert isinstance(parsed, dict)
        block = _single_code_block(parsed)
        assert block["parameter_keys"] == ["search_location", "provider_first_name"]
        assert "str(search_location)" in block["code"]
        assert "str(provider_first_name)" in block["code"]
        assert "address_or_postal_code" not in block["code"]
        assert result["data"]["imposed_substitutions"]["parameter_aliases"] == {
            "address_or_postal_code": "search_location"
        }

    def test_identifier_rewrite_skips_string_literals_and_comments(self) -> None:
        source = (
            'await page.locator("#providerSearch").fill(str(provider_query))\n'
            "# provider_query should stay readable in comments\n"
            'message = "provider_query should stay readable in strings"\n'
        )

        rewritten = workflow_update_module._replace_python_identifier(
            source,
            "provider_query",
            "provider_name",
        )

        ast.parse(rewritten)
        assert 'await page.locator("#providerSearch").fill(str(provider_name))' in rewritten
        assert "# provider_query should stay readable in comments" in rewritten
        assert '"provider_query should stay readable in strings"' in rewritten

    def test_identifier_rewrite_preserves_multiline_block_shape(self) -> None:
        source = (
            "if provider_query:\n"
            '    await page.locator("#providerSearch").fill(str(provider_query))\n'
            "else:\n"
            '    await page.locator("#providerSearch").fill("")\n'
        )

        rewritten = workflow_update_module._replace_python_identifier(source, "provider_query", "provider_name")

        assert rewritten == (
            "if provider_name:\n"
            '    await page.locator("#providerSearch").fill(str(provider_name))\n'
            "else:\n"
            '    await page.locator("#providerSearch").fill("")\n'
        )

    @pytest.mark.asyncio
    async def test_single_local_string_constant_is_lifted_for_synthesized_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = self._provider_search_ctx()

        result = await _update_workflow({"workflow_yaml": _SUBMITTED_LOCAL_CONSTANT_YAML}, ctx)

        assert result["ok"] is True
        parsed = parse_workflow_yaml(ctx.workflow_yaml)
        assert isinstance(parsed, dict)
        block = _single_code_block(parsed)
        assert 'await page.locator("#provInput").fill(str(provider_name))' in block["code"]
        assert block["parameter_keys"] == ["provider_name"]
        assert parsed["workflow_definition"]["parameters"] == [
            {
                "parameter_type": "workflow",
                "workflow_parameter_type": "string",
                "key": "provider_name",
                "default_value": "Sample Search",
            }
        ]

    @pytest.mark.asyncio
    async def test_promotes_scouted_typed_literal_across_multiple_and_nested_code_blocks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = self._typed_default_ctx()
        ctx.scout_trajectory = [self._cafe_search_capture(), {**ctx.scout_trajectory[0], "trajectory_index": 1}]

        result = await _update_workflow({"workflow_yaml": _SUBMITTED_TYPED_LITERAL_REWRITE_YAML}, ctx)

        assert result["ok"] is True
        parsed = parse_workflow_yaml(ctx.workflow_yaml)
        assert isinstance(parsed, dict)
        blocks = {str(block.get("label")): block for block in workflow_blocks(parsed)}
        assert "fill(str(search))" in blocks["search_catalog"]["code"]
        assert "type(str(search))" in blocks["select_result"]["code"]
        assert "fill(str(search))" in blocks["nested_search"]["code"]
        assert '"example_sku_123"' in blocks["verify_cart"]["code"]
        assert blocks["search_catalog"]["parameter_keys"] == ["existing_filter", "search"]
        assert blocks["select_result"]["parameter_keys"] == ["search"]
        assert blocks["nested_search"]["parameter_keys"] == ["search"]
        assert parsed["workflow_definition"]["parameters"] == [
            {
                "parameter_type": "workflow",
                "workflow_parameter_type": "string",
                "key": "existing_filter",
                "default_value": "active",
            },
            {
                "parameter_type": "workflow",
                "workflow_parameter_type": "string",
                "key": "search",
                "default_value": "example_sku_123",
            },
        ]

    @pytest.mark.asyncio
    async def test_scouted_typed_default_without_literal_rewrite_does_not_create_orphan_input(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = self._typed_default_ctx()
        ctx.scout_trajectory = [self._cafe_search_capture(), {**ctx.scout_trajectory[0], "trajectory_index": 1}]
        submitted_yaml = _SUBMITTED_TYPED_LITERAL_REWRITE_YAML.replace("example_sku_123", "other_sku_456")

        result = await _update_workflow({"workflow_yaml": submitted_yaml}, ctx)

        assert result["ok"] is True
        parsed = parse_workflow_yaml(ctx.workflow_yaml)
        assert isinstance(parsed, dict)
        assert parsed["workflow_definition"]["parameters"] == [
            {
                "parameter_type": "workflow",
                "workflow_parameter_type": "string",
                "key": "existing_filter",
                "default_value": "active",
            }
        ]
        for block in workflow_blocks(parsed):
            if str(block.get("block_type") or "").lower() == "code":
                assert "other_sku_456" in str(block.get("code") or "")
                assert "str(search)" not in str(block.get("code") or "")

    def _role_name_nav_download_ctx(self) -> CopilotContext:
        ctx = _code_only_ctx()
        _enable_imposition(ctx)
        ctx.scout_trajectory = [
            {
                "tool_name": "click",
                "selector": "#statement-row",
                "source_url": "https://example.com/billing",
                "trajectory_index": 0,
            },
            {
                "tool_name": "click",
                "selector": "a",
                "source_url": "https://example.com/billing",
                "role": "link",
                "accessible_name": "View Printable Statement",
            },
        ]
        ctx.reached_download_target = ReachedDownloadTarget(
            selector='a[href="/billing/statement.pdf"]',
            affordance_text="View Printable Statement",
            download_kind="attribute",
            source_step="trajectory_recency",
            already_registered=False,
        )
        return ctx

    def test_role_name_bare_nav_click_imposes_with_emitted_get_by_role(self) -> None:
        ctx = self._role_name_nav_download_ctx()
        workflow_yaml = _yaml(
            """
            title: Statement download
            workflow_definition:
              blocks:
              - block_type: code
                label: download_statement
                code: |
                  await page.locator("#statement-row").click()
            """
        )

        result = workflow_update_module._maybe_impose_synthesized_code_block(workflow_yaml, ctx)

        assert result.violations == []
        block = _single_code_block(parse_workflow_yaml(result.workflow_yaml))
        assert 'await page.get_by_role("link", name="View Printable Statement", exact=True).click()' in block["code"]
        assert "async with page.expect_download()" in block["code"]
        assert "/billing/statement.pdf" in block["code"]

    def test_anchorless_bare_nav_click_still_blocks_imposition(self) -> None:
        ctx = self._role_name_nav_download_ctx()
        ctx.scout_trajectory[1].pop("role")
        ctx.scout_trajectory[1].pop("accessible_name")
        workflow_yaml = _yaml(
            """
            title: Statement download
            workflow_definition:
              blocks:
              - block_type: code
                label: download_statement
                code: |
                  await page.locator("#statement-row").click()
            """
        )

        result = workflow_update_module._maybe_impose_synthesized_code_block(workflow_yaml, ctx)

        assert any("ambiguous_bare_selector" in violation for violation in result.violations)

    def test_provenance_gate_admits_self_validating_aria_role_name(self) -> None:
        entry = {
            "trajectory_index": 1,
            "selector": "a",
            "emitted_literal": _get_by_role_expr_strict("link", "View Statements"),
            "source": "aria_role_name",
            "role": "link",
            "name": "View Statements",
        }
        assert workflow_update_module._locator_provenance_is_self_validating(entry) is True

    def test_provenance_gate_rejects_tampered_aria_role_name(self) -> None:
        tampered_literal = {
            "selector": "a",
            "emitted_literal": 'page.get_by_role("link", name="Spoofed")',
            "source": "aria_role_name",
            "role": "link",
            "name": "View Statements",
        }
        tampered_role = {
            "selector": "a",
            "emitted_literal": _get_by_role_expr_strict("link", "View Statements"),
            "source": "aria_role_name",
            "role": "button",
            "name": "View Statements",
        }
        assert workflow_update_module._locator_provenance_is_self_validating(tampered_literal) is False
        assert workflow_update_module._locator_provenance_is_self_validating(tampered_role) is False

    def test_provenance_gate_keeps_selector_byte_equality(self) -> None:
        assert (
            workflow_update_module._locator_provenance_is_self_validating(
                {"selector": "#row", "emitted_literal": "#row", "source": "selector"}
            )
            is True
        )
        assert (
            workflow_update_module._locator_provenance_is_self_validating(
                {"selector": "#row", "emitted_literal": "#other", "source": "selector"}
            )
            is False
        )
        assert (
            workflow_update_module._locator_provenance_is_self_validating(
                {"selector": "#row", "emitted_literal": "#row", "source": "first_fallback"}
            )
            is False
        )

    def test_provenance_gate_admits_self_validating_exact_aria_role_name(self) -> None:
        entry = {
            "trajectory_index": 1,
            "selector": "a",
            "emitted_literal": _get_by_role_expr_strict("link", "Download"),
            "source": "aria_role_name",
            "role": "link",
            "name": "Download",
        }
        assert workflow_update_module._locator_provenance_is_self_validating(entry) is True

    def test_provenance_gate_rejects_non_exact_aria_role_name_literal(self) -> None:
        tampered = {
            "selector": "a",
            "emitted_literal": _get_by_role_expr("link", "Download"),
            "source": "aria_role_name",
            "role": "link",
            "name": "Download",
        }
        assert workflow_update_module._locator_provenance_is_self_validating(tampered) is False


def test_direct_literal_rewrite_preserves_unicode_prefix_offsets() -> None:
    code = textwrap.dedent(
        """
        await page.locator("#café-search-résumé").fill("example_sku_123")
        await page.locator("#naïve-search").type("example_sku_123")
        """
    ).strip()

    rewritten, used_keys = workflow_update_module._rewrite_direct_literal_fills(code, {"example_sku_123": "search"})

    assert used_keys == ["search"]
    assert (
        rewritten
        == textwrap.dedent(
            """
        await page.locator("#café-search-résumé").fill(str(search))
        await page.locator("#naïve-search").type(str(search))
        """
        ).strip()
    )


def test_literal_binding_sees_through_first_last_disambiguator() -> None:
    code = textwrap.dedent(
        """
        await page.locator("input").first.fill("example_sku_123")
        await page.get_by_role("textbox").last.type("example_sku_123")
        """
    ).strip()

    assert workflow_update_module._submitted_fill_type_arguments(code) == ["example_sku_123", "example_sku_123"]

    rewritten, used_keys = workflow_update_module._rewrite_direct_literal_fills(code, {"example_sku_123": "search"})
    assert used_keys == ["search"]
    assert 'await page.locator("input").first.fill(str(search))' in rewritten
    assert 'await page.get_by_role("textbox").last.type(str(search))' in rewritten


def test_python_ast_offsets_are_utf8_byte_offsets_for_unicode_source() -> None:
    code = 'await page.locator("#café-search-résumé").fill("example_sku_123")'
    tree = workflow_update_module._wrapped_code_ast(code)
    assert tree is not None
    fill_call = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "fill"
        and node.args
    )
    literal = fill_call.args[0]
    assert isinstance(literal, ast.Constant)
    assert literal.value == "example_sku_123"

    prefix = 'await page.locator("#café-search-résumé").fill('
    assert literal.col_offset == 4 + len(prefix.encode("utf-8"))
    assert literal.col_offset != 4 + len(prefix)
    assert workflow_update_module._AST_COLUMN_OFFSETS_ARE_UTF8_BYTES is True


class TestSeamSalvageIntoContext:
    @pytest.mark.asyncio
    async def test_stale_entry_dropped_and_draft_metadata_survives_unaccepted_submission(self) -> None:
        draft_yaml = _yaml(
            """
            title: Registry lookup
            workflow_definition:
              blocks:
              - block_type: code
                label: block_a
                code: |
                  await page.goto("https://example.com/search")
              - block_type: code
                label: block_b
                code: |
                  await page.goto("https://example.com/results")
            """
        )
        submitted_yaml = _yaml(
            """
            title: Registry lookup
            workflow_definition:
              blocks:
              - block_type: code
                label: block_a
                code: |
                  await page.goto("https://example.com/search")
            """
        )
        ctx = _code_only_ctx()
        ctx.workflow_yaml = draft_yaml
        stored_block_b = _terminal_metadata("block_b", "expand the result rows")
        ctx.code_artifact_metadata = {
            "block_a": _terminal_metadata("block_a", "search the registry"),
            "block_b": stored_block_b,
        }
        ctx.workflow_verification_evidence.code_artifact_metadata = dict(ctx.code_artifact_metadata)
        metadata = [
            _terminal_metadata("block_a", "search the registry"),
            _terminal_metadata("ghost", "does not exist"),
        ]

        result = await _update_workflow({"workflow_yaml": submitted_yaml, "code_artifact_metadata": metadata}, ctx)

        # The stale entry is pruned at the seam; the submission proceeds and
        # only non-metadata gates may reject it, so prior-draft metadata stays.
        error_text = str(result.get("error") or "")
        assert "Artifact metadata" not in error_text
        assert "ghost" not in error_text
        assert sorted(ctx.code_artifact_metadata) == ["block_a", "block_b"]
        assert ctx.code_artifact_metadata["block_b"] == stored_block_b
        assert ctx.workflow_verification_evidence.code_artifact_metadata == ctx.code_artifact_metadata

    @pytest.mark.asyncio
    async def test_minimal_metadata_with_trajectory_produces_no_violation_error(self) -> None:
        ctx = _code_only_ctx()
        metadata = [_terminal_metadata("search_registry", "search the registry")]
        result = await _update_workflow(
            {"workflow_yaml": _SAFE_EXTRACTION_CODE_YAML, "code_artifact_metadata": metadata}, ctx
        )
        # The seam may reject later (credential checks need the app); the metadata
        # contract itself must not be the rejection.
        error_text = str(result.get("error") or "")
        assert "Artifact metadata" not in error_text
        assert "contract violation" not in error_text
        assert ctx.code_artifact_metadata["search_registry"]["artifact_id"] == "code_artifact:search_registry"


class TestStaleLabelSeamFlow:
    @pytest.mark.asyncio
    async def test_stale_metadata_label_rekeys_without_any_stale_rejection(self) -> None:
        # Run-2 shape: metadata keyed to a label absent from the submitted
        # YAML. The seam re-keys it to the only uncovered code block, so
        # neither the metadata gate nor the stale-block-metadata validation
        # path can bounce the submission back to the model.
        ctx = _code_only_ctx()
        ctx.workflow_yaml = _SAFE_EXTRACTION_CODE_YAML
        metadata = [_terminal_metadata("search_certificant_stale", "search the registry")]

        result = await _update_workflow(
            {"workflow_yaml": _SAFE_EXTRACTION_CODE_YAML, "code_artifact_metadata": metadata}, ctx
        )

        error_text = str(result.get("error") or "")
        assert "Artifact metadata" not in error_text
        assert "still appears stale" not in error_text
        assert list(ctx.code_artifact_metadata.keys()) == ["search_registry"]
        assert ctx.code_artifact_metadata["search_registry"]["artifact_id"] == "code_artifact:search_registry"
        # The seam never rewrites YAML labels, so its output cannot trip the
        # stale-block-metadata validation that fires on label/title renames.
        assert _detect_stale_block_metadata(_SAFE_EXTRACTION_CODE_YAML, ctx.workflow_yaml) == []

    @pytest.mark.asyncio
    async def test_malformed_per_entry_refs_normalize_without_scout_interactions(self) -> None:
        # Run-3 shape: model-authored observation_refs rows missing the scoped
        # id, authored before any scout interaction was recorded.
        ctx = _code_only_ctx()
        ctx.scout_trajectory = []
        metadata = [
            {
                **_terminal_metadata("search_registry", "search the registry"),
                "observation_refs": [{"observation_ref": "obs1", "status": "observed_not_verified"}],
            }
        ]

        result = await _update_workflow(
            {"workflow_yaml": _SAFE_EXTRACTION_CODE_YAML, "code_artifact_metadata": metadata}, ctx
        )

        error_text = str(result.get("error") or "")
        assert "Artifact metadata" not in error_text
        assert "contract violation" not in error_text
        ref = ctx.code_artifact_metadata["search_registry"]["observation_refs"][0]
        assert ref["dependency_id"]
        assert ref["source_tool"]


class TestCredentialScoutPersistGate:
    _MULTI_BLOCK_TARGETED_CREDENTIAL_YAML = _yaml(
        """
        title: Saved credential login
        workflow_definition:
          parameters:
          - parameter_type: workflow
            workflow_parameter_type: credential_id
            key: login_credential
            default_value: cred_missing
          blocks:
          - block_type: code
            label: enter_username
            parameter_keys: [login_credential]
            code: |
              await page.locator("#email").fill(login_credential.username)
              await page.locator("#continue").click()
          - block_type: code
            label: sign_in_to_business_center
            parameter_keys: [login_credential]
            code: |
              await page.locator("input[type='password']").fill(login_credential.password)
              await page.locator("#sign-in").click()
          - block_type: code
            label: open_matching_statement
            code: |
              text = await page.locator("table").inner_text()
              print(text)
        """
    )
    _SUBMIT_CODE_YAML = _credential_code_yaml(
        code="""
        await page.locator("#email").fill(login_credential.username)
        await page.locator("input[type='password']").fill(login_credential.password)
        await page.locator("#totpmfa").fill(login_credential.totp)
        await page.locator("input[type='submit']").click()
        await page.wait_for_load_state("load")
        """
    )
    _FILL_ONLY_CODE_YAML = _credential_code_yaml(
        code="""
        await page.locator("#email").fill(login_credential.username)
        await page.locator("input[type='password']").fill(login_credential.password)
        await page.locator("#totpmfa").fill(login_credential.totp)
        """
    )
    _UNSAFE_SUBMIT_CODE_YAML = _credential_code_yaml(
        code="""
        leaked = page.__class__
        await page.locator("#email").fill(login_credential.username)
        await page.locator("input[type='password']").fill(login_credential.password)
        await page.locator("input[type='submit']").click()
        """
    )
    _RUNTIME_OTP_CODE_YAML = _credential_code_yaml(
        code="""
        await page.locator("#email").fill(login_credential.username)
        await page.locator("input[type='password']").fill(login_credential.password)
        await page.locator("#totpmfa").fill(await login_credential.otp())
        await page.locator("input[type='submit']").click()
        await page.wait_for_load_state("load")
        """
    )
    _DRAFT_DOWNLOAD_CODE_YAML = _credential_code_yaml(
        code="""
        await page.goto("https://example.com/login")
        await page.locator("#email").fill(login_credential.username)
        await page.locator("input[type='password']").fill(login_credential.password)
        await page.locator("#totpmfa").fill(await login_credential.otp())
        await page.locator("input[type='submit']").click()
        await page.wait_for_load_state("load")
        async with page.expect_download() as download_info:
            await page.locator("a[href='/invoices/monthly.pdf']").click()
        download = await download_info.value
        print(download.suggested_filename)
        """
    )
    _DOWNLOAD_CODE_YAML = _yaml(
        """
        title: Download invoice
        workflow_definition:
          blocks:
          - block_type: code
            label: download_monthly_invoice_pdf
            code: |
              async with page.expect_download() as download_info:
                  await page.locator("a[href='/invoices/monthly.pdf']").click()
              download = await download_info.value
              print(download.suggested_filename)
        """
    )

    @pytest.mark.asyncio
    async def test_rejects_credential_submit_code_without_matching_fill_scouts(self) -> None:
        ctx = _code_only_ctx()
        ctx.scout_trajectory = []
        ctx.last_code_authoring_repair_context = _stale_unresolved_repair_context()

        result = await _update_workflow({"workflow_yaml": self._SUBMIT_CODE_YAML}, ctx)

        assert result["ok"] is False
        assert "fill_credential_field" in result["error"]
        assert "click the submit control or press Enter" in result["error"]
        assert "authoring_repair_context" not in result["data"]
        assert ctx.last_code_authoring_repair_context is None
        assert result["user_facing_summary"] == (
            "I need to verify the saved-credential login in the browser before I can save or run this code."
        )

    @pytest.mark.asyncio
    async def test_credential_scout_blocker_takes_precedence_over_code_safety(self) -> None:
        ctx = _code_only_ctx()
        ctx.scout_trajectory = []

        result = await _update_workflow({"workflow_yaml": self._UNSAFE_SUBMIT_CODE_YAML}, ctx)

        assert result["ok"] is False
        assert "fill_credential_field" in result["error"]
        assert "Insecure code detected" not in result["error"]
        assert result["user_facing_summary"] == (
            "I need to verify the saved-credential login in the browser before I can save or run this code."
        )
        assert result["data"]["failure_type"] == "missing_credential_or_init"
        code_safety_diagnostics = result["data"]["diagnostic_code_safety_errors"]
        assert any("private methods or attributes" in error for error in code_safety_diagnostics)

    @pytest.mark.asyncio
    async def test_allows_submit_code_gate_once_matching_fills_and_submit_are_scouted(self) -> None:
        ctx = _code_only_ctx()
        ctx.scout_trajectory = [
            _credential_fill_interaction("username"),
            _credential_fill_interaction("password"),
            _credential_fill_interaction("totp"),
            _submit_interaction(),
        ]

        result = await _update_workflow({"workflow_yaml": self._SUBMIT_CODE_YAML}, ctx)

        assert result["ok"] is False
        error_text = str(result.get("error") or "")
        assert "was not found in this organization" in error_text
        assert "fill_credential_field" not in error_text
        assert "saved-credential login flow" not in error_text

    @pytest.mark.asyncio
    async def test_submit_code_still_requires_later_submit_after_matching_fills(self) -> None:
        ctx = _code_only_ctx()
        ctx.scout_trajectory = [
            _credential_fill_interaction("username"),
            _credential_fill_interaction("password"),
            _credential_fill_interaction("totp"),
        ]

        result = await _update_workflow({"workflow_yaml": self._SUBMIT_CODE_YAML}, ctx)

        assert result["ok"] is False
        assert "later submit action on the same page" in result["error"]
        assert "click the submit control or press Enter" in result["error"]

    @pytest.mark.asyncio
    async def test_fill_only_code_requires_matching_fill_scouts_but_not_submit(self) -> None:
        ctx = _code_only_ctx()
        ctx.scout_trajectory = [
            _credential_fill_interaction("username"),
            _credential_fill_interaction("password"),
            _credential_fill_interaction("totp"),
        ]

        result = await _update_workflow({"workflow_yaml": self._FILL_ONLY_CODE_YAML}, ctx)

        assert result["ok"] is False
        error_text = str(result.get("error") or "")
        assert "was not found in this organization" in error_text
        assert "click the submit control or press Enter" not in error_text
        assert "saved-credential login flow" not in error_text

    @pytest.mark.asyncio
    async def test_runtime_otp_method_does_not_require_impossible_live_otp_fill(self) -> None:
        ctx = _code_only_ctx()
        ctx.scout_trajectory = [
            _credential_fill_interaction("username"),
            _credential_fill_interaction("password"),
            _submit_interaction(),
        ]

        result = await _update_workflow({"workflow_yaml": self._RUNTIME_OTP_CODE_YAML}, ctx)

        assert result["ok"] is False
        error_text = str(result.get("error") or "")
        assert "was not found in this organization" in error_text
        assert "successful `fill_credential_field` scouting for `totp`" not in error_text
        assert "saved-credential login flow" not in error_text

    @pytest.mark.asyncio
    async def test_draft_only_credential_code_download_persists_without_scouts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _draft_only_credential_ctx()

        result = await _update_workflow({"workflow_yaml": self._DRAFT_DOWNLOAD_CODE_YAML}, ctx)

        assert result["ok"] is True
        assert "login_with_saved_credential" in ctx.workflow_yaml
        assert "expect_download" in ctx.workflow_yaml
        assert "login_credential.username" in ctx.workflow_yaml

    @pytest.mark.asyncio
    async def test_standard_mode_behavior_is_unchanged(self) -> None:
        ctx = _standard_ctx()
        ctx.scout_trajectory = []

        result = await _update_workflow({"workflow_yaml": self._SUBMIT_CODE_YAML}, ctx)

        assert result["ok"] is False
        error_text = str(result.get("error") or "")
        assert "fill_credential_field" not in error_text
        assert "saved-credential login flow" not in error_text

    @pytest.mark.asyncio
    async def test_targeted_run_labels_scope_credential_scout_gate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        ctx.scout_trajectory = []

        accepted = await _update_workflow(
            {
                "workflow_yaml": self._MULTI_BLOCK_TARGETED_CREDENTIAL_YAML,
                "block_labels": ["open_matching_statement"],
            },
            ctx,
            allow_missing_credentials=True,
        )

        assert accepted["ok"] is True

        selected_credential_ctx = _code_only_ctx()
        selected_credential_ctx.scout_trajectory = []
        rejected = await _update_workflow(
            {
                "workflow_yaml": self._MULTI_BLOCK_TARGETED_CREDENTIAL_YAML,
                "block_labels": ["sign_in_to_business_center"],
            },
            selected_credential_ctx,
        )

        assert rejected["ok"] is False
        assert rejected["data"]["failure_type"] == "missing_credential_or_init"
        assert "sign_in_to_business_center" in rejected["error"]
        assert rejected["user_facing_summary"] == CREDENTIAL_SCOUT_VERIFY_REPLY

    @pytest.mark.asyncio
    async def test_persisted_parameter_shape_does_not_rescout_unchanged_selected_credential_block(self) -> None:
        sign_in_code = """
        await page.locator("#email").fill(login_credential.username)
        await page.locator("input[type='password']").fill(login_credential.password)
        await page.locator("#sign-in").click()
        """
        sign_in_code = textwrap.dedent(sign_in_code).strip()
        login_parameter = {
            "parameter_type": "workflow",
            "workflow_parameter_type": "credential_id",
            "key": "login_credential",
            "default_value": "cred_missing",
        }
        prior_yaml = yaml.safe_dump(
            {
                "title": "Saved credential login",
                "workflow_definition": {
                    "parameters": [login_parameter],
                    "blocks": [
                        {
                            "block_type": "code",
                            "label": "sign_in_to_business_center",
                            "parameters": [login_parameter],
                            "code": sign_in_code,
                        }
                    ],
                },
            },
            sort_keys=False,
        )
        submitted_yaml = yaml.safe_dump(
            {
                "title": "Saved credential login",
                "workflow_definition": {
                    "parameters": [
                        login_parameter,
                        {
                            "parameter_type": "workflow",
                            "workflow_parameter_type": "string",
                            "key": "account_number",
                            "default_value": "100245",
                        },
                    ],
                    "blocks": [
                        {
                            "block_type": "code",
                            "label": "sign_in_to_business_center",
                            "parameter_keys": ["login_credential"],
                            "code": sign_in_code,
                        },
                        {
                            "block_type": "code",
                            "label": "open_matching_statement",
                            "parameter_keys": ["account_number"],
                            "code": 'await page.get_by_text("View Printable Statement").wait_for(timeout=5000)',
                        },
                    ],
                },
            },
            sort_keys=False,
        )
        ctx = _code_only_ctx()
        ctx.workflow_yaml = prior_yaml
        ctx.scout_trajectory = []

        result = await _update_workflow(
            {
                "workflow_yaml": submitted_yaml,
                "block_labels": ["sign_in_to_business_center", "open_matching_statement"],
            },
            ctx,
        )

        assert result["ok"] is False
        error_text = str(result.get("error") or "")
        assert "open_matching_statement" in error_text
        assert "fill_credential_field" not in error_text

        changed_yaml = submitted_yaml.replace(
            'await page.locator("#sign-in").click()',
            'await page.locator("#sign-in").click()\n      await page.locator("#post-login").click()',
        )
        changed_ctx = _code_only_ctx()
        changed_ctx.workflow_yaml = prior_yaml
        changed_ctx.scout_trajectory = []

        changed_result = await _update_workflow(
            {
                "workflow_yaml": changed_yaml,
                "block_labels": ["sign_in_to_business_center"],
            },
            changed_ctx,
        )

        assert changed_result["ok"] is False
        assert changed_result["data"]["failure_type"] == "missing_credential_or_init"
        assert changed_result["user_facing_summary"] == CREDENTIAL_SCOUT_VERIFY_REPLY


def test_run_id_leak_check_covers_non_numeric_ids() -> None:
    with pytest.raises(ValueError):
        assert_clean_user_facing_text("Outcome uncertain for wr_sample_123abc.")


class TestStripRedundantSandboxImports:
    @pytest.mark.parametrize(
        ("code", "expected_module"),
        [
            ("import asyncio\nawait page.goto('https://example.com')", "asyncio"),
            ("import asyncio\nawait asyncio.sleep(1)", "asyncio"),
            ("import json\nvalue = json.dumps({})", "json"),
            ("import json\nvalue = json.loads('{}')", "json"),
            ("import re\nmatch = re.search(r'x', 'x')", "re"),
            ("import html\nvalue = html.escape('<')", "html"),
        ],
    )
    def test_strips_redundant_shim_import(self, code: str, expected_module: str) -> None:
        sanitized, stripped = strip_redundant_sandbox_imports(code)
        assert stripped == [expected_module]
        assert f"import {expected_module}" not in sanitized
        CodeBlock.is_safe_code(sanitized)

    @pytest.mark.parametrize(
        "code",
        [
            "import asyncio\nawait asyncio.gather(page.goto('https://example.com'))",
            "import json\ntry:\n    json.loads('x')\nexcept json.JSONDecodeError:\n    pass",
            "import html\nvalue = html.unescape('&amp;')",
            "import re\nvalue = re.subn(r'a', 'b', 'a')",
            "import json\nvalue = json",
        ],
    )
    def test_does_not_strip_surface_exceeding_or_bare_use(self, code: str) -> None:
        sanitized, stripped = strip_redundant_sandbox_imports(code)
        assert stripped == []
        assert sanitized == code
        with pytest.raises(InsecureCodeDetected):
            CodeBlock.is_safe_code(sanitized)

    @pytest.mark.parametrize(
        "code",
        [
            "import os as json\nvalue = json",
            "import json.decoder\nvalue = 1",
            "from re import search\nmatch = search(r'x', 'x')",
            "import json; value = json.dumps({})",
            "import requests\nvalue = requests",
            "import os\nvalue = 1",
            'import re; import os\nresult = os.environ.get("AWS_SECRET_ACCESS_KEY")',
            'import os; import re\nresult = os.environ.get("AWS_SECRET_ACCESS_KEY")',
            'import json; import requests\nresult = requests.get("https://example.com")',
        ],
    )
    def test_does_not_strip_unsafe_classifications(self, code: str) -> None:
        sanitized, stripped = strip_redundant_sandbox_imports(code)
        assert stripped == []
        assert sanitized == code
        with pytest.raises(InsecureCodeDetected):
            CodeBlock.is_safe_code(sanitized)

    def test_preserves_surrounding_comments(self) -> None:
        code = "import asyncio  # drop me\n# keep this comment\nawait asyncio.sleep(1)  # trailing"
        sanitized, stripped = strip_redundant_sandbox_imports(code)
        assert stripped == ["asyncio"]
        assert "# keep this comment" in sanitized
        assert "# trailing" in sanitized
        assert "import asyncio" not in sanitized

    def test_syntax_error_is_returned_unchanged(self) -> None:
        code = "import asyncio\nawait page.goto("
        sanitized, stripped = strip_redundant_sandbox_imports(code)
        assert stripped == []
        assert sanitized == code

    def test_shim_surface_is_derived_from_build_safe_vars(self) -> None:
        expected = {
            name: frozenset(vars(value))
            for name, value in CodeBlock.build_safe_vars().items()
            if isinstance(value, SimpleNamespace)
        }
        assert _sandbox_shim_surface() == expected

    def test_blocked_attrs_are_not_a_strippable_surface(self) -> None:
        surface_attrs = {attr for attrs in _sandbox_shim_surface().values() for attr in attrs}
        assert surface_attrs.isdisjoint(CodeBlock.BLOCKED_ATTRS)


class TestStripRedundantSandboxImportsInYaml:
    def test_malformed_yaml_is_returned_unchanged(self) -> None:
        malformed = "title: [unterminated\n"
        sanitized, stripped = _strip_redundant_sandbox_imports_in_yaml(malformed)
        assert stripped == []
        assert sanitized == malformed

    def test_non_workflow_yaml_is_returned_unchanged(self) -> None:
        non_workflow = "just: a mapping\n"
        sanitized, stripped = _strip_redundant_sandbox_imports_in_yaml(non_workflow)
        assert stripped == []
        assert sanitized == non_workflow

    def test_multi_block_strips_per_block(self) -> None:
        multi_block = _yaml(
            """
            title: Multi
            workflow_definition:
              blocks:
              - block_type: code
                label: first
                code: |
                  import asyncio
                  await asyncio.sleep(1)
              - block_type: code
                label: second
                code: |
                  await page.goto("https://example.com")
              - block_type: code
                label: third
                code: |
                  import json
                  value = json.dumps({})
            """
        )
        sanitized, stripped = _strip_redundant_sandbox_imports_in_yaml(multi_block)
        assert sorted(stripped) == ["asyncio", "json"]
        assert "import asyncio" not in sanitized
        assert "import json" not in sanitized

    def test_no_change_returns_original_text(self) -> None:
        sanitized, stripped = _strip_redundant_sandbox_imports_in_yaml(_SAFE_CODE_YAML)
        assert stripped == []
        assert sanitized == _SAFE_CODE_YAML


def _distinct_guardrail_yaml(index: int) -> str:
    bodies = [
        f"value = undefined_helper_{index}()",
        f'import os\nawait page.goto("https://example.com/{index}")',
        f'await page.evaluate("{index} + 1")',
    ]
    return _code_yaml(bodies[index % len(bodies)])


def _distinct_credential_collision_yaml(index: int) -> str:
    unsafe = [
        f"value = undefined_helper_{index}()",
        "import os",
        f'await page.evaluate("{index} + 1")',
    ]
    return _credential_code_yaml(
        code=f"""
        await page.locator("#email").fill(login_credential.username)
        await page.locator("input[type='password']").fill(login_credential.password)
        {unsafe[index % len(unsafe)]}
        """
    )


def _page_evaluate_credential_collision_yaml(index: int) -> str:
    return _distinct_credential_collision_yaml((index * 3) + 2)


def _safe_credential_collision_yaml(index: int) -> str:
    return _credential_code_yaml(
        code=f"""
        await page.locator("#email").fill(login_credential.username)
        await page.locator("input[type='password']").fill(login_credential.password)
        landing_url_{index} = "https://example.com/portal/{index}"
        """
    )


def _terminal_challenge_signal() -> CopilotToolBlockerSignal:
    return CopilotToolBlockerSignal(
        blocker_kind="tool_error",
        agent_steering_text="A site verification challenge blocked the run.",
        user_facing_reason="The site's verification challenge blocked the run.",
        recovery_hint="report_blocker_to_user",
        internal_reason_code=TERMINAL_CHALLENGE_BLOCKER_REASON_CODE,
        blocked_tool="update_and_run_blocks",
    )


class TestCodeAuthoringGuardrailChurnBackstop:
    @pytest.mark.asyncio
    async def test_counter_climbs_through_credential_scout_branch(self) -> None:
        ctx = _code_only_ctx()
        ctx.scout_trajectory = []
        unsafe_credential_yaml = _credential_code_yaml(
            code="""
            import os
            await page.locator("#email").fill(login_credential.username)
            await page.locator("input[type='password']").fill(login_credential.password)
            await page.locator("input[type='submit']").click()
            """
        )

        result = await _update_workflow({"workflow_yaml": unsafe_credential_yaml}, ctx)

        assert result["ok"] is False
        assert result["data"]["failure_type"] == "missing_credential_or_init"
        assert ctx.code_authoring_guardrail_reject_count == 1

    @pytest.mark.asyncio
    async def test_clean_accept_does_not_climb_counter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()

        result = await _update_workflow({"workflow_yaml": _SAFE_CODE_YAML}, ctx)

        assert result["ok"] is True
        assert ctx.code_authoring_guardrail_reject_count == 0

    @pytest.mark.asyncio
    async def test_mixed_credential_and_unresolved_name_reject_returns_code_repair_progress(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        info_calls: list[tuple[str, dict[str, str | list[str]]]] = []

        def capture_info(event: str, **kwargs: str | list[str]) -> None:
            info_calls.append((event, kwargs))

        monkeypatch.setattr(workflow_update_module.LOG, "info", capture_info)
        ctx = _code_only_ctx()
        ctx.scout_trajectory = []

        result = await _update_workflow({"workflow_yaml": _distinct_credential_collision_yaml(0)}, ctx)

        assert ctx.code_authoring_guardrail_reject_count == 1
        assert result["ok"] is False
        assert result["data"]["surface_kind"] == "code_repair_progress"
        assert "failure_type" not in result["data"]
        repair_context = result["data"]["authoring_repair_context"]
        assert repair_context["reason_code"] == SANDBOX_UNRESOLVED_NAME_REASON_CODE
        assert repair_context["block_label"] == "login_with_saved_credential"
        assert repair_context["unresolved_names"] == ["undefined_helper_0"]
        assert result["user_facing_summary"] != CREDENTIAL_SCOUT_VERIFY_REPLY
        assert ctx.last_code_authoring_reject_was_credential_priority is False
        assert ctx.blocker_signal is None
        assert ctx.latest_tool_blocker_signal is None
        assert (
            "copilot code authoring repair context stored",
            {
                "reason_code": SANDBOX_UNRESOLVED_NAME_REASON_CODE,
                "block_label": "login_with_saved_credential",
                "unresolved_names": ["undefined_helper_0"],
                "parameter_keys": [],
                "available_parameter_keys": [],
                "binding_candidates": ["undefined_helper_0"],
            },
        ) in info_calls

    @pytest.mark.asyncio
    async def test_single_credential_priority_reject_defers_to_credential_scout_reply(self) -> None:
        ctx = _code_only_ctx()
        ctx.scout_trajectory = []

        result = await _update_workflow({"workflow_yaml": _distinct_credential_collision_yaml(1)}, ctx)

        assert ctx.code_authoring_guardrail_reject_count == 1
        assert result["ok"] is False
        assert result["data"]["failure_type"] == "missing_credential_or_init"
        assert result["user_facing_summary"] == CREDENTIAL_SCOUT_VERIFY_REPLY
        assert ctx.blocker_signal is None
        assert ctx.latest_tool_blocker_signal is None

    @pytest.mark.asyncio
    async def test_mixed_credential_and_non_name_guardrail_uses_credential_priority_path(self) -> None:
        ctx = _code_only_ctx()
        ctx.scout_trajectory = []

        result = await _update_workflow({"workflow_yaml": _distinct_credential_collision_yaml(1)}, ctx)

        assert result["ok"] is False
        assert result["data"]["failure_type"] == "missing_credential_or_init"
        assert result["user_facing_summary"] == CREDENTIAL_SCOUT_VERIFY_REPLY
        assert ctx.last_code_authoring_repair_context is None
        assert ctx.last_code_authoring_reject_was_credential_priority is True

    @pytest.mark.asyncio
    async def test_standard_policy_mixed_credential_and_unresolved_name_omits_repair_context(self) -> None:
        ctx = _standard_ctx()
        ctx.scout_trajectory = []

        result = await _update_workflow({"workflow_yaml": _distinct_credential_collision_yaml(0)}, ctx)

        assert result["ok"] is False
        assert "authoring_repair_context" not in result["data"]
        assert ctx.last_code_authoring_repair_context is None

    @pytest.mark.asyncio
    async def test_single_pure_credential_reject_defers_to_credential_scout_reply(self) -> None:
        ctx = _code_only_ctx()
        ctx.scout_trajectory = []

        result = await _update_workflow({"workflow_yaml": _safe_credential_collision_yaml(0)}, ctx)

        assert ctx.code_authoring_guardrail_reject_count == 1
        assert result["ok"] is False
        assert result["data"]["failure_type"] == "missing_credential_or_init"
        assert "diagnostic_code_safety_errors" not in result["data"]
        assert result["user_facing_summary"] == CREDENTIAL_SCOUT_VERIFY_REPLY
        assert ctx.blocker_signal is None
        assert ctx.latest_tool_blocker_signal is None

    @pytest.mark.asyncio
    async def test_unchanged_persisted_credential_block_does_not_require_new_scout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        prior_yaml = _safe_credential_collision_yaml(0)
        ctx = _code_only_ctx()
        ctx.workflow_yaml = prior_yaml
        ctx.scout_trajectory = []

        result = await _update_workflow({"workflow_yaml": prior_yaml}, ctx, allow_missing_credentials=True)

        assert result["ok"] is True
        assert result.get("user_facing_summary") != CREDENTIAL_SCOUT_VERIFY_REPLY
        assert ctx.code_authoring_guardrail_reject_count == 0

    @pytest.mark.asyncio
    async def test_changed_persisted_credential_block_still_requires_new_scout(self) -> None:
        ctx = _code_only_ctx()
        ctx.workflow_yaml = _safe_credential_collision_yaml(0)
        ctx.scout_trajectory = []

        result = await _update_workflow({"workflow_yaml": _safe_credential_collision_yaml(1)}, ctx)

        assert result["ok"] is False
        assert result["data"]["failure_type"] == "missing_credential_or_init"
        assert result["user_facing_summary"] == CREDENTIAL_SCOUT_VERIFY_REPLY


_RESALE_URL = "https://example.com/orders"
_QUOTE_URL = "https://example.com/quote"


def _resale_ctx(*, refiner_selector: str = 'button[data-action="status"]') -> CopilotContext:
    ctx = _code_only_ctx()
    _enable_imposition(ctx)
    ctx.scout_trajectory = [
        {
            "tool_name": "type_text",
            "selector": "#order-id",
            "source_url": _RESALE_URL,
            "typed_length": 6,
            "typed_value": "abc123",
            "role": "textbox",
            "accessible_name": "Order ID",
            "trajectory_index": 0,
        },
        {
            "tool_name": "click",
            "selector": "button",
            "source_url": _RESALE_URL,
            "trajectory_index": 1,
        },
        {
            "tool_name": "click",
            "selector": refiner_selector,
            "source_url": _RESALE_URL,
            "trajectory_index": 2,
        },
    ]
    return ctx


def _resale_submitted_yaml(refiner_selector: str = 'button[data-action="status"]') -> str:
    escaped = refiner_selector.replace('"', '\\"')
    return _yaml(
        f"""
        title: Order status
        workflow_definition:
          blocks:
          - block_type: code
            label: order_status
            code: |
              await page.locator("#order-id").fill(str(order_id))
              await page.locator("{escaped}").click()
        """
    )


def _quote_ctx() -> CopilotContext:
    ctx = _code_only_ctx()
    _enable_imposition(ctx)
    ctx.scout_trajectory = [
        {
            "tool_name": "type_text",
            "selector": "#zip",
            "source_url": _QUOTE_URL,
            "typed_length": 5,
            "typed_value": "02110",
            "role": "textbox",
            "accessible_name": "ZIP code",
            "trajectory_index": 0,
        },
        {
            "tool_name": "click",
            "selector": "#continue",
            "source_url": _QUOTE_URL,
            "trajectory_index": 1,
        },
        {
            "tool_name": "click",
            "selector": "#coverage-next",
            "source_url": "https://example.com/quote/coverage",
            "trajectory_index": 2,
        },
    ]
    return ctx


def _author_time_reject_outcome(reason_code: BuildTestOutcomeReasonCode) -> RecordedBuildTestOutcome:
    return recorded_outcome_from_author_time_reject(
        reason_code=reason_code,
        attempted_block_label=workflow_update_module._SYNTHESIZED_BLOCK_LABEL,
        block_labels=[workflow_update_module._SYNTHESIZED_BLOCK_LABEL],
        structural_payload={
            "reason_code": reason_code,
            "block_label": workflow_update_module._SYNTHESIZED_BLOCK_LABEL,
        },
        observed_evidence_summary=reason_code,
    )


def _code_blocks(parsed: dict[str, object]) -> dict[str, dict[str, object]]:
    blocks = [block for block in workflow_blocks(parsed) if str(block.get("block_type") or "").lower() == "code"]
    return {str(block.get("label") or ""): block for block in blocks}


def _submitted_with_sibling_code(sibling_code: str) -> str:
    indented = textwrap.indent(textwrap.dedent(sibling_code).strip(), " " * 14)
    return _yaml(
        f"""
        title: Quote
        workflow_definition:
          blocks:
          - block_type: code
            label: {workflow_update_module._SYNTHESIZED_BLOCK_LABEL}
            code: |
              await page.locator("#zip").fill(str(zip_code))
              await page.locator("#continue").click()
          - block_type: code
            label: preserved_code
            code: |
{indented}
        """
    )


_METADATA_LESS_DIRECTIVE_EVENT = "copilot_output_contract_spine_structure_directive_emitted"

_RECORDED_METADATA_REJECT_BLOCK_LABEL = "validate_public_start_service_path"


def _recorded_metadata_reject_yaml() -> str:
    return _yaml(
        f"""
        title: Provider lookup
        workflow_definition:
          parameters:
          - {{parameter_type: output, key: workflow_output}}
          blocks:
          - block_type: code
            label: {_RECORDED_METADATA_REJECT_BLOCK_LABEL}
            code: |
              await page.locator("#start").click()
        """
    )


def _metadata_less_output_yaml(selector: str = "#search-submit") -> str:
    return _yaml(
        f"""
        title: Provider lookup
        workflow_definition:
          parameters:
          - {{parameter_type: output, key: workflow_output}}
          blocks:
          - block_type: code
            label: extract_provider
            code: |
              await page.locator("{selector}").click()
        """
    )


def _metadata_less_page_read_yaml() -> str:
    return _yaml(
        """
        title: Provider lookup
        workflow_definition:
          parameters:
          - {parameter_type: output, key: workflow_output}
          blocks:
          - block_type: code
            label: extract_provider
            code: |
              await page.locator("#search-submit").click()
              value = await page.locator("#npi").inner_text()
              return {"npi": value}
        """
    )


def _metadata_less_two_block_yaml() -> str:
    return _yaml(
        """
        title: Provider lookup
        workflow_definition:
          parameters:
          - {parameter_type: output, key: workflow_output}
          blocks:
          - block_type: code
            label: first_block
            description: extract the provider data
            code: |
              await page.locator("#a").click()
          - block_type: code
            label: second_block
            description: extract the license data
            code: |
              await page.locator("#b").click()
        """
    )


class TestWholeTrajectoryImposition:
    def test_imposes_over_unscouted_browser_fill_in_selected_block(self) -> None:
        ctx = _quote_ctx()
        submitted = _yaml(
            """
            title: Quote
            workflow_definition:
              blocks:
              - block_type: code
                label: quote_flow
                code: |
                  await page.locator("#zip").fill(str(zip_code))
                  await page.locator("#continue").click()
                  await page.locator("#electricDate").fill("2026-07-01")
                  await page.locator("#coverage-next").click()
            """
        )

        result = workflow_update_module._maybe_impose_synthesized_code_block(submitted, ctx)

        assert result.violations == []
        assert result.substitutions is not None
        code = str(_single_code_block(parse_workflow_yaml(result.workflow_yaml))["code"])
        assert "#electricDate" not in code
        assert code.index('page.locator("#zip")') < code.index('page.locator("#continue")')
        assert code.index('page.locator("#continue")') < code.index('page.locator("#coverage-next")')

    def test_entry_opener_drop_is_instrumented_not_silently_forgiven(self) -> None:
        ctx = _quote_ctx()
        ctx.scout_trajectory.insert(
            0,
            {"tool_name": "click", "selector": "button", "source_url": _QUOTE_URL, "trajectory_index": 0},
        )
        submitted = _yaml(
            """
            title: Quote
            workflow_definition:
              blocks:
              - block_type: code
                label: quote_flow
                code: |
                  await page.locator("#zip").fill(str(zip_code))
                  await page.locator("#continue").click()
                  await page.locator("#electricDate").fill("2026-07-01")
                  await page.locator("#coverage-next").click()
            """
        )

        with capture_logs() as logs:
            result = workflow_update_module._maybe_impose_synthesized_code_block(submitted, ctx)

        assert result.violations == []
        assert result.substitutions is not None
        forgiven = result.substitutions["forgiven_entry_opener_drops"]
        assert [record["reason_code"] for record in forgiven] == ["ambiguous_bare_selector"]
        assert forgiven[0]["forgiveness"] == "entry_opener_superseded_by_locator_provenance"
        assert any(entry.get("event") == "copilot_imposition_forgave_entry_opener_drop" for entry in logs)

    def test_imposes_over_unscouted_selected_block_extra_click(self) -> None:
        ctx = _quote_ctx()
        submitted = _yaml(
            """
            title: Quote
            workflow_definition:
              blocks:
              - block_type: code
                label: quote_flow
                code: |
                  await page.locator("#zip").fill(str(zip_code))
                  await page.locator("#continue").click()
                  await page.locator("#electricDate").click()
                  await page.locator("#coverage-next").click()
            """
        )

        result = workflow_update_module._maybe_impose_synthesized_code_block(submitted, ctx)

        assert result.violations == []
        assert result.substitutions is not None
        code = str(_single_code_block(parse_workflow_yaml(result.workflow_yaml))["code"])
        assert "#electricDate" not in code
        assert 'page.locator("#zip")' in code
        assert 'page.locator("#continue")' in code
        assert 'page.locator("#coverage-next")' in code

    def test_p10_shaped_selected_surplus_browser_mutations_are_discarded(self) -> None:
        ctx = _quote_ctx()
        submitted = _yaml(
            """
            title: Quote
            workflow_definition:
              blocks:
              - block_type: code
                label: quote_flow
                code: |
                  await page.locator("#zip").fill(str(zip_code))
                  await page.locator("#continue").click()
                  await page.locator("#electricPlan").select_option("basic")
                  await page.locator("#electricDate").fill("2026-07-01")
                  await page.locator("#coverage-next").click()
            """
        )

        result = workflow_update_module._maybe_impose_synthesized_code_block(submitted, ctx)

        assert result.violations == []
        assert result.substitutions is not None
        code = str(_single_code_block(parse_workflow_yaml(result.workflow_yaml))["code"])
        assert "#electricPlan" not in code
        assert "#electricDate" not in code
        assert result.substitutions["source_trajectory_count"] == 3
        assert code.index('page.locator("#zip")') < code.index('page.locator("#continue")')
        assert code.index('page.locator("#continue")') < code.index('page.locator("#coverage-next")')

    def test_wrong_selected_block_receiver_is_overwritten_by_scout_spine(self) -> None:
        ctx = _quote_ctx()
        submitted = _yaml(
            """
            title: Quote
            workflow_definition:
              blocks:
              - block_type: code
                label: quote_flow
                code: |
                  await page.locator("#wrongZip").fill(str(zip_code))
                  await page.locator("#continue").click()
                  await page.locator("#coverage-next").click()
            """
        )

        result = workflow_update_module._maybe_impose_synthesized_code_block(submitted, ctx)

        assert result.violations == []
        assert result.substitutions is not None
        code = str(_single_code_block(parse_workflow_yaml(result.workflow_yaml))["code"])
        assert "#wrongZip" not in code
        assert 'page.locator("#zip")' in code

    def test_selected_alias_locator_extra_is_discarded_by_imposition(self) -> None:
        ctx = _quote_ctx()
        submitted = _yaml(
            """
            title: Quote
            workflow_definition:
              blocks:
              - block_type: code
                label: quote_flow
                code: |
                  await page.locator("#zip").fill(str(zip_code))
                  await page.locator("#continue").click()
                  target = page.locator("#electricDate")
                  await target.fill("2026-07-01")
                  await page.locator("#coverage-next").click()
            """
        )

        result = workflow_update_module._maybe_impose_synthesized_code_block(submitted, ctx)

        assert result.violations == []
        assert result.substitutions is not None
        code = str(_single_code_block(parse_workflow_yaml(result.workflow_yaml))["code"])
        assert "#electricDate" not in code
        assert 'target = page.locator("#electricDate")' not in code
        assert 'page.locator("#zip")' in code
        assert 'page.locator("#coverage-next")' in code

    def test_selected_helper_extra_is_discarded_by_imposition(self) -> None:
        ctx = _quote_ctx()
        submitted = _yaml(
            """
            title: Quote
            workflow_definition:
              blocks:
              - block_type: code
                label: quote_flow
                code: |
                  await page.locator("#zip").fill(str(zip_code))
                  await page.locator("#continue").click()
                  async def clear(target):
                      await target.evaluate("node => node.remove()")
                  await clear(target=page.locator("#electricDate"))
                  await page.locator("#coverage-next").click()
            """
        )

        result = workflow_update_module._maybe_impose_synthesized_code_block(submitted, ctx)

        assert result.violations == []
        assert result.substitutions is not None
        code = str(_single_code_block(parse_workflow_yaml(result.workflow_yaml))["code"])
        assert "#electricDate" not in code
        assert "async def clear" not in code

    def test_selected_dynamic_extra_is_discarded_by_imposition(self) -> None:
        ctx = _quote_ctx()
        submitted = _yaml(
            """
            title: Quote
            workflow_definition:
              blocks:
              - block_type: code
                label: quote_flow
                code: |
                  await page.locator("#zip").fill(str(zip_code))
                  await page.locator("#continue").click()
                  await getattr(page.locator("#electricDate"), "fill")("2026-07-01")
                  await page.locator("#coverage-next").click()
            """
        )

        result = workflow_update_module._maybe_impose_synthesized_code_block(submitted, ctx)

        assert result.violations == []
        assert result.substitutions is not None
        code = str(_single_code_block(parse_workflow_yaml(result.workflow_yaml))["code"])
        assert "#electricDate" not in code
        assert "getattr" not in code

    def test_rejects_extra_changed_block_with_unscouted_browser_mutation(self) -> None:
        ctx = _records_spine_ctx()
        submitted = _yaml(
            f"""
            title: Records
            workflow_definition:
              blocks:
              - block_type: code
                label: {workflow_update_module._SYNTHESIZED_BLOCK_LABEL}
                code: |
                  await page.locator("#stage-a").click()
                  await page.locator("#stage-b").click()
                  await page.locator("#stage-c").click()
              - block_type: code
                label: invented_browser_step
                code: |
                  await page.locator("#electricDate").fill("2026-07-01")
            """
        )

        with capture_logs() as logs:
            result = workflow_update_module._maybe_impose_synthesized_code_block(submitted, ctx)

        assert ctx.spine_imposition_owned_attempt is False
        assert any("unscouted browser action" in violation for violation in result.violations)
        assert any("never_captured" in violation for violation in result.violations)
        assert result.substitutions is None
        events = [log for log in logs if log["event"] == "copilot_browser_surface_rejection_provenance"]
        assert events and events[0]["kind"] == "never_captured"

    @pytest.mark.parametrize(
        "sibling_code",
        [
            pytest.param(
                """
            await page.evaluate("window.localStorage.clear()")
            """,
                id="test_rejects_unknown_page_receiver_call",
            ),
            pytest.param(
                """
            await page.locator("#electricDate").evaluate("node => node.remove()")
            """,
                id="test_rejects_unknown_direct_locator_receiver_call",
            ),
            pytest.param(
                """
            target = page.locator("#electricDate")
            await target.evaluate("node => node.remove()")
            """,
                id="test_rejects_unknown_locator_alias_receiver_call",
            ),
            pytest.param(
                """
            p = page
            await p.goto("https://example.com/other")
            """,
                id="test_rejects_page_alias_mutation",
            ),
            pytest.param(
                """
            p = page
            q = p
            await q.goto("https://example.com/other")
            """,
                id="test_rejects_transitive_page_alias_mutation",
            ),
            pytest.param(
                """
            target = page.locator("#electricDate")
            other = target
            await other.fill("2026-07-01")
            """,
                id="test_rejects_transitive_locator_alias_mutation",
            ),
            pytest.param(
                """
            fill_electric = page.locator("#electricDate").fill
            await fill_electric("2026-07-01")
            """,
                id="test_rejects_bound_method_alias_mutation",
            ),
            pytest.param(
                """
            fill_electric = page.locator("#electricDate").fill
            other = fill_electric
            await other("2026-07-01")
            """,
                id="test_rejects_transitive_bound_method_alias_mutation",
            ),
            pytest.param(
                """
            await getattr(page, "goto")("https://example.com/other")
            """,
                id="test_rejects_dynamic_dispatch_on_page",
            ),
            pytest.param(
                """
            target = page.locator("#electricDate")
            await getattr(target, "fill")("2026-07-01")
            """,
                id="test_rejects_dynamic_dispatch_on_locator_alias",
            ),
            pytest.param(
                """
            async def clear(target):
                await target.evaluate("node => node.remove()")
            await clear(page.locator("#electricDate"))
            """,
                id="test_rejects_helper_receiving_browser_object",
            ),
            pytest.param(
                """
            async def clear(target):
                await target.evaluate("node => node.remove()")
            await clear(target=page.locator("#electricDate"))
            """,
                id="test_rejects_helper_receiving_browser_keyword_object",
            ),
            pytest.param(
                """
            async def navigate(page_arg):
                await page_arg.goto("https://example.com/other")
            await navigate(page_arg=page)
            """,
                id="test_rejects_helper_receiving_page_keyword_object",
            ),
        ],
    )
    def test_owned_attempt_drops_ambiguous_only_sibling(self, sibling_code: str) -> None:
        ctx = _quote_ctx()
        submitted = _submitted_with_sibling_code(sibling_code)

        with capture_logs() as logs:
            result = workflow_update_module._maybe_impose_synthesized_code_block(submitted, ctx)

        assert result.violations == []
        assert result.substitutions is not None
        blocks = _code_blocks(parse_workflow_yaml(result.workflow_yaml))
        assert "preserved_code" not in blocks
        dropped = [log for log in logs if log["event"] == "copilot_spine_stale_rung_dropped"]
        assert dropped and dropped[0]["dropped_labels"] == ["preserved_code"]
        assert dropped[0]["dropped_actions"]

    def test_unowned_attempt_still_rejects_ambiguous_browser_mutation(self) -> None:
        ctx = _records_spine_ctx()
        submitted = _yaml(
            f"""
            title: Records
            workflow_definition:
              blocks:
              - block_type: code
                label: {workflow_update_module._SYNTHESIZED_BLOCK_LABEL}
                code: |
                  await page.locator("#stage-a").click()
              - block_type: code
                label: helper_stage
                code: |
                  target = page.locator("#stage-z")
                  await getattr(target, "fill")("2026-07-01")
            """
        )

        result = workflow_update_module._maybe_impose_synthesized_code_block(submitted, ctx)

        assert ctx.spine_imposition_owned_attempt is False
        assert any("ambiguous browser action" in violation for violation in result.violations)
        assert result.substitutions is None

    def test_preserves_simple_extraction_only_block(self) -> None:
        ctx = _quote_ctx()
        submitted = _yaml(
            f"""
            title: Quote
            workflow_definition:
              blocks:
              - block_type: code
                label: {workflow_update_module._SYNTHESIZED_BLOCK_LABEL}
                code: |
                  await page.locator("#zip").fill(str(zip_code))
                  await page.locator("#continue").click()
              - block_type: code
                label: summarize_quote
                code: |
                  heading = await page.locator("h1").inner_text()
                  return {{"heading": heading}}
            """
        )

        result = workflow_update_module._maybe_impose_synthesized_code_block(submitted, ctx)

        assert result.violations == []
        assert result.substitutions is not None
        blocks = _code_blocks(parse_workflow_yaml(result.workflow_yaml))
        assert "#coverage-next" in str(blocks[workflow_update_module._SYNTHESIZED_BLOCK_LABEL]["code"])
        assert str(blocks["summarize_quote"]["code"]).strip() == (
            'heading = await page.locator("h1").inner_text()\nreturn {"heading": heading}'
        )

    def test_preserves_read_only_selected_extraction_suffix_after_exact_spine(self) -> None:
        ctx = _quote_ctx()
        label = workflow_update_module._SYNTHESIZED_BLOCK_LABEL
        metadata = _terminal_metadata(label, "quote")
        metadata["claimed_outcomes"][0]["goal_value_paths"] = ["heading"]
        metadata["terminal_verifier_expectations"][0]["goal_value_paths"] = ["heading"]
        ctx.raw_code_artifact_metadata = [metadata]
        synthesized = workflow_update_module.synthesize_code_block(ctx.scout_trajectory, strict_selectors=True)
        assert synthesized is not None
        submitted_code = (
            textwrap.dedent(synthesized.code).rstrip()
            + '\nheading = await page.locator("h1").inner_text()\nreturn {"heading": heading}\n'
        )
        submitted = _yaml(
            f"""
            title: Quote
            workflow_definition:
              blocks:
              - block_type: code
                label: {label}
                code: |
{textwrap.indent(submitted_code, " " * 18)}
            """
        )

        result = workflow_update_module._maybe_impose_synthesized_code_block(submitted, ctx)

        assert result.violations == []
        assert result.substitutions is not None
        assert result.substitutions["preserved_extraction_suffix"] is True
        parsed = parse_workflow_yaml(result.workflow_yaml)
        code_blocks = [block for block in workflow_blocks(parsed) if block.get("block_type") == "code"]
        assert len(code_blocks) > 1
        assert code_blocks[-1]["label"] == label
        output_code = str(code_blocks[-1]["code"]).strip()
        assert output_code == 'heading = await page.locator("h1").inner_text()\nreturn {"heading": heading}'
        browser_code = "\n".join(str(block.get("code") or "") for block in code_blocks[:-1])
        assert 'await page.locator("#coverage-next").click()' in browser_code
        assert all("heading" not in str(block.get("code") or "") for block in code_blocks[:-1])

    def test_preserves_page_read_only_selected_extraction_suffix_after_exact_spine(self) -> None:
        ctx = _quote_ctx()
        label = workflow_update_module._SYNTHESIZED_BLOCK_LABEL
        metadata = _terminal_metadata(label, "quote")
        metadata["claimed_outcomes"][0]["goal_value_paths"] = ["heading"]
        metadata["terminal_verifier_expectations"][0]["goal_value_paths"] = ["heading"]
        ctx.raw_code_artifact_metadata = [metadata]
        synthesized = workflow_update_module.synthesize_code_block(ctx.scout_trajectory, strict_selectors=True)
        assert synthesized is not None
        submitted_code = (
            textwrap.dedent(synthesized.code).rstrip()
            + '\nheading = await page.inner_text("h1")\nreturn {"heading": heading}\n'
        )
        submitted = _yaml(
            f"""
            title: Quote
            workflow_definition:
              blocks:
              - block_type: code
                label: {label}
                code: |
{textwrap.indent(submitted_code, " " * 18)}
            """
        )

        result = workflow_update_module._maybe_impose_synthesized_code_block(submitted, ctx)

        assert result.violations == []
        assert result.substitutions is not None
        assert result.substitutions["preserved_extraction_suffix"] is True
        parsed = parse_workflow_yaml(result.workflow_yaml)
        code_blocks = [block for block in workflow_blocks(parsed) if block.get("block_type") == "code"]
        assert len(code_blocks) > 1
        assert code_blocks[-1]["label"] == label
        output_code = str(code_blocks[-1]["code"]).strip()
        assert output_code == 'heading = await page.inner_text("h1")\nreturn {"heading": heading}'
        browser_code = "\n".join(str(block.get("code") or "") for block in code_blocks[:-1])
        assert 'await page.locator("#coverage-next").click()' in browser_code
        assert all("heading" not in str(block.get("code") or "") for block in code_blocks[:-1])

    def test_rejects_selected_extraction_suffix_browser_mutation(self) -> None:
        ctx = _quote_ctx()
        synthesized = workflow_update_module.synthesize_code_block(ctx.scout_trajectory, strict_selectors=True)
        assert synthesized is not None
        submitted_code = (
            textwrap.dedent(synthesized.code).rstrip() + '\nawait page.locator("#electricDate").fill("2026-07-01")\n'
        )
        submitted = _yaml(
            f"""
            title: Quote
            workflow_definition:
              blocks:
              - block_type: code
                label: {workflow_update_module._SYNTHESIZED_BLOCK_LABEL}
                code: |
{textwrap.indent(submitted_code, " " * 18)}
            """
        )

        result = workflow_update_module._maybe_impose_synthesized_code_block(submitted, ctx)

        assert any(
            "extraction suffix contains unscouted browser action" in violation for violation in result.violations
        )
        assert result.substitutions is None

    def test_rejects_selected_extraction_suffix_alias_browser_mutation(self) -> None:
        ctx = _quote_ctx()
        synthesized = workflow_update_module.synthesize_code_block(ctx.scout_trajectory, strict_selectors=True)
        assert synthesized is not None
        submitted_code = (
            textwrap.dedent(synthesized.code).rstrip()
            + '\ntarget = page.locator("#electricDate")\nawait target.fill("2026-07-01")\n'
        )
        submitted = _yaml(
            f"""
            title: Quote
            workflow_definition:
              blocks:
              - block_type: code
                label: {workflow_update_module._SYNTHESIZED_BLOCK_LABEL}
                code: |
{textwrap.indent(submitted_code, " " * 18)}
            """
        )

        result = workflow_update_module._maybe_impose_synthesized_code_block(submitted, ctx)

        assert any(
            "extraction suffix contains ambiguous browser action" in violation for violation in result.violations
        )
        assert result.substitutions is None

    def test_owned_attempt_drops_ambiguous_helper_browser_mutation(self) -> None:
        ctx = _quote_ctx()
        submitted = _yaml(
            f"""
            title: Quote
            workflow_definition:
              blocks:
              - block_type: code
                label: {workflow_update_module._SYNTHESIZED_BLOCK_LABEL}
                code: |
                  await page.locator("#zip").fill(str(zip_code))
                  await page.locator("#continue").click()
              - block_type: code
                label: helper_step
                code: |
                  async def fill(locator, value):
                      await locator.fill(value)
                  target = page.locator("#electricDate")
                  await fill(target, "2026-07-01")
            """
        )

        result = workflow_update_module._maybe_impose_synthesized_code_block(submitted, ctx)

        assert result.violations == []
        assert "helper_step" not in _code_blocks(parse_workflow_yaml(result.workflow_yaml))

    def test_multi_screen_trajectory_persists_in_order_with_proven_locators(self) -> None:
        ctx = _quote_ctx()
        submitted = _yaml(
            f"""
            title: Quote
            workflow_definition:
              blocks:
              - block_type: code
                label: {workflow_update_module._SYNTHESIZED_BLOCK_LABEL}
                code: |
                  await page.locator("#zip").fill(str(zip_code))
                  await page.locator("#continue").click()
            """
        )

        result = workflow_update_module._maybe_impose_synthesized_code_block(submitted, ctx)

        assert result.violations == []
        assert result.substitutions is not None
        block = _single_code_block(parse_workflow_yaml(result.workflow_yaml))
        code = str(block["code"])
        assert code.index('page.locator("#zip")') < code.index('page.locator("#continue")')
        assert code.index('page.locator("#continue")') < code.index('page.locator("#coverage-next")')

    @pytest.mark.asyncio
    async def test_changed_selected_browser_action_args_do_not_preserve_submitted_extraction(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _quote_ctx()
        synthesized = workflow_update_module.synthesize_code_block(ctx.scout_trajectory, strict_selectors=True)
        assert synthesized is not None
        submitted_code = textwrap.dedent(synthesized.code).replace(
            'await page.locator("#zip").fill(str(zip_code))',
            'await page.locator("#zip").fill("99999")',
        )
        submitted = _yaml(
            f"""
            title: Quote
            workflow_definition:
              blocks:
              - block_type: code
                label: {workflow_update_module._SYNTHESIZED_BLOCK_LABEL}
                parameter_keys: [zip_code]
                code: |
{textwrap.indent(submitted_code, " " * 18)}
            """
        )

        result = await _update_workflow(
            {
                "workflow_yaml": submitted,
                "code_artifact_metadata": [
                    _terminal_metadata(workflow_update_module._SYNTHESIZED_BLOCK_LABEL, "quote")
                ],
            },
            ctx,
        )

        assert result["ok"] is True
        parsed = parse_workflow_yaml(ctx.workflow_yaml)
        assert isinstance(parsed, dict)
        code = str(_single_code_block(parsed)["code"])
        assert 'await page.locator("#zip").fill(str(zip_code))' in code
        assert 'await page.locator("#zip").fill("99999")' not in code

    @pytest.mark.asyncio
    async def test_alias_reconciled_selected_spine_preserves_submitted_extraction(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _quote_ctx()
        synthesized = workflow_update_module.synthesize_code_block(ctx.scout_trajectory, strict_selectors=True)
        assert synthesized is not None
        label = workflow_update_module._SYNTHESIZED_BLOCK_LABEL
        submitted_code = (
            textwrap.dedent(synthesized.code).replace("zip_code", "postal_code").rstrip()
            + f'\n{label}_output = {{"quote": "Q-001"}}\nreturn {label}_output\n'
        )
        submitted = _yaml(
            f"""
            title: Quote
            workflow_definition:
              parameters:
              - {{parameter_type: workflow, workflow_parameter_type: string, key: postal_code, default_value: "02110"}}
              blocks:
              - block_type: code
                label: {label}
                parameter_keys: [postal_code]
                code: |
{textwrap.indent(submitted_code, " " * 18)}
            """
        )

        result = await _update_workflow(
            {
                "workflow_yaml": submitted,
                "code_artifact_metadata": [
                    {
                        **_terminal_metadata(label, "quote"),
                        "claimed_outcomes": [
                            {
                                **_terminal_metadata(label, "quote")["claimed_outcomes"][0],
                                "goal_value_paths": ["quote"],
                            }
                        ],
                        "terminal_verifier_expectations": [
                            {
                                **_terminal_metadata(label, "quote")["terminal_verifier_expectations"][0],
                                "goal_value_paths": ["quote"],
                            }
                        ],
                    }
                ],
            },
            ctx,
        )

        assert result["ok"] is True
        substitutions = result["data"]["imposed_substitutions"]
        assert substitutions["preserved_submitted_extraction_code"] is True
        assert substitutions["parameter_aliases"] == {"zip_code": "postal_code"}
        assert "scrubbed_stale_selected_goal_value_paths" not in substitutions
        parsed = parse_workflow_yaml(ctx.workflow_yaml)
        assert isinstance(parsed, dict)
        code_blocks = [block for block in workflow_blocks(parsed) if block.get("block_type") == "code"]
        assert len(code_blocks) > 1
        block = _code_blocks(parsed)[label]
        browser_code = "\n".join(str(stage.get("code") or "") for stage in code_blocks[:-1])
        for stage in code_blocks[:-1]:
            expected_keys = ["postal_code"] if "postal_code" in str(stage.get("code") or "") else None
            assert stage.get("parameter_keys") == expected_keys
        assert any(stage.get("parameter_keys") == ["postal_code"] for stage in code_blocks[:-1])
        assert "postal_code" in browser_code
        assert "zip_code" not in browser_code
        code = str(block["code"])
        assert f"return {label}_output" in code
        assert "postal_code" not in code
        artifact = ctx.code_artifact_metadata[label]
        assert workflow_update_module._artifact_declares_goal_values(artifact)

    @pytest.mark.asyncio
    async def test_metadata_selected_extraction_only_imposes_scout_spine(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _quote_ctx()
        submitted = _yaml(
            f"""
            title: Quote
            workflow_definition:
              blocks:
              - block_type: code
                label: {workflow_update_module._SYNTHESIZED_BLOCK_LABEL}
                code: |
                  records = [{{"number": "Q-001"}}]
            """
        )

        result = await _update_workflow(
            {
                "workflow_yaml": submitted,
                "code_artifact_metadata": [
                    _terminal_metadata(workflow_update_module._SYNTHESIZED_BLOCK_LABEL, "quote result")
                ],
            },
            ctx,
        )

        assert result["ok"] is True
        parsed = parse_workflow_yaml(ctx.workflow_yaml)
        assert isinstance(parsed, dict)
        code_blocks = [block for block in workflow_blocks(parsed) if block.get("block_type") == "code"]
        assert len(code_blocks) > 1
        block = _code_blocks(parsed)[workflow_update_module._SYNTHESIZED_BLOCK_LABEL]
        code = "\n".join(str(stage.get("code") or "") for stage in code_blocks[:-1])
        output_code = str(block["code"])
        assert code_blocks[-1]["label"] == workflow_update_module._SYNTHESIZED_BLOCK_LABEL
        assert result["data"]["imposed_substitutions"]["source_trajectory_count"] == 3
        assert 'await page.locator("#zip").fill(str(zip_code))' in code
        assert 'await page.locator("#continue").click()' in code
        assert 'await page.locator("#coverage-next").click()' in code
        assert 'records = [{"number": "Q-001"}]' in output_code
        assert 'return {"records": records}' in output_code
        assert all("records" not in str(stage.get("code") or "") for stage in code_blocks[:-1])

    @pytest.mark.asyncio
    async def test_author_metadata_reject_reopens_changed_collapsed_code_block(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _quote_ctx()
        ctx.update_workflow_called = True
        ctx.latest_recorded_build_test_outcome = _author_time_reject_outcome("metadata_reject")
        ctx.workflow_yaml = _yaml(
            f"""
            title: Quote
            workflow_definition:
              blocks:
              - block_type: code
                label: {workflow_update_module._SYNTHESIZED_BLOCK_LABEL}
                code: |
                  records = [{{"quote": "old"}}]
            """
        )
        submitted = _yaml(
            f"""
            title: Quote
            workflow_definition:
              blocks:
              - block_type: code
                label: {workflow_update_module._SYNTHESIZED_BLOCK_LABEL}
                code: |
                  records = [{{"number": "Q-001"}}]
            """
        )

        result = await _update_workflow(
            {
                "workflow_yaml": submitted,
                "code_artifact_metadata": [
                    _terminal_metadata(workflow_update_module._SYNTHESIZED_BLOCK_LABEL, "quote result")
                ],
            },
            ctx,
        )

        assert result["ok"] is True
        parsed = parse_workflow_yaml(ctx.workflow_yaml)
        assert isinstance(parsed, dict)
        code_blocks = [block for block in workflow_blocks(parsed) if block.get("block_type") == "code"]
        block = _code_blocks(parsed)[workflow_update_module._SYNTHESIZED_BLOCK_LABEL]
        browser_code = "\n".join(str(stage.get("code") or "") for stage in code_blocks[:-1])
        code = str(block["code"])
        assert result["data"]["imposed_substitutions"]["source_trajectory_count"] == 3
        assert result["data"]["imposed_substitutions"]["separated_browser_stage_count"] > 1
        assert code_blocks[-1]["label"] == workflow_update_module._SYNTHESIZED_BLOCK_LABEL
        assert 'await page.locator("#zip").fill(str(zip_code))' in browser_code
        assert 'await page.locator("#continue").click()' in browser_code
        assert 'await page.locator("#coverage-next").click()' in browser_code
        assert 'records = [{"number": "Q-001"}]' not in browser_code
        assert 'records = [{"number": "Q-001"}]' in code
        assert 'return {"records": records}' in code

    def test_author_ambiguous_selector_reject_reopens_strict_imposition_with_typed_repair_context(self) -> None:
        ctx = _resale_ctx()
        ctx.update_workflow_called = True
        ctx.latest_recorded_build_test_outcome = _author_time_reject_outcome("code_safety_reject")
        ctx.last_code_authoring_repair_context = CodeAuthoringRepairContext(
            block_label="order_status",
            reason_code="ambiguous_bare_selector",
            selector="button",
        )

        result = workflow_update_module._maybe_impose_synthesized_code_block(_resale_submitted_yaml(), ctx)

        assert result.violations == []
        assert result.substitutions is not None
        block = _single_code_block(parse_workflow_yaml(result.workflow_yaml))
        assert 'button[data-action=\\"status\\"]' in str(block["code"])

    def test_author_schema_incompatibility_does_not_reopen_collapsed_code_block(self) -> None:
        ctx = _quote_ctx()
        ctx.update_workflow_called = True
        ctx.synthesized_goal_complete_landed = True
        ctx.latest_recorded_build_test_outcome = _author_time_reject_outcome("schema_incompatibility")
        ctx.workflow_yaml = _yaml(
            f"""
            title: Quote
            workflow_definition:
              blocks:
              - block_type: code
                label: {workflow_update_module._SYNTHESIZED_BLOCK_LABEL}
                code: |
                  records = [{{"quote": "old"}}]
            """
        )
        submitted = _yaml(
            f"""
            title: Quote
            workflow_definition:
              blocks:
              - block_type: code
                label: {workflow_update_module._SYNTHESIZED_BLOCK_LABEL}
                code: |
                  records = [{{"quote": "pending"}}]
            """
        )

        result = workflow_update_module._maybe_impose_synthesized_code_block(submitted, ctx)

        assert result.violations == []
        assert result.substitutions is None
        assert result.workflow_yaml == submitted

    @pytest.mark.asyncio
    async def test_metadata_selected_page_goto_extra_is_discarded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _quote_ctx()
        submitted = _yaml(
            """
            title: Quote
            workflow_definition:
              blocks:
              - block_type: code
                label: quote_flow
                code: |
                  await page.goto("https://example.com/other")
                  records = [{"number": "Q-001"}]
            """
        )

        result = await _update_workflow(
            {
                "workflow_yaml": submitted,
                "code_artifact_metadata": [_terminal_metadata("quote_flow", "quote result")],
            },
            ctx,
        )

        assert result["ok"] is True
        parsed = parse_workflow_yaml(ctx.workflow_yaml)
        assert isinstance(parsed, dict)
        code = str(_single_code_block(parsed)["code"])
        assert 'page.goto("https://example.com/other")' not in code
        assert 'records = [{"number": "Q-001"}]' not in code
        assert 'await page.locator("#zip").fill(str(zip_code))' in code
        assert result["data"]["imposed_substitutions"]["scrubbed_stale_selected_goal_value_paths"] is True

    @pytest.mark.asyncio
    async def test_valid_selected_extraction_suffix_keeps_goal_path_metadata(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _quote_ctx()
        synthesized = workflow_update_module.synthesize_code_block(ctx.scout_trajectory, strict_selectors=True)
        assert synthesized is not None
        submitted_code = textwrap.dedent(synthesized.code).rstrip() + '\nreturn {"records": [{"number": "Q-001"}]}\n'
        submitted = _yaml(
            f"""
            title: Quote
            workflow_definition:
              blocks:
              - block_type: code
                label: {workflow_update_module._SYNTHESIZED_BLOCK_LABEL}
                code: |
{textwrap.indent(submitted_code, " " * 18)}
            """
        )

        result = await _update_workflow(
            {
                "workflow_yaml": submitted,
                "code_artifact_metadata": [
                    _terminal_metadata(workflow_update_module._SYNTHESIZED_BLOCK_LABEL, "quote result")
                ],
            },
            ctx,
        )

        assert result["ok"] is True
        assert result["data"]["imposed_substitutions"]["preserved_extraction_suffix"] is True
        artifact = ctx.code_artifact_metadata[workflow_update_module._SYNTHESIZED_BLOCK_LABEL]
        assert workflow_update_module._artifact_declares_goal_values(artifact)

    @pytest.mark.asyncio
    async def test_p9_opaque_self_authored_extraction_metadata_is_preserved(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _quote_ctx()
        synthesized = workflow_update_module.synthesize_code_block(ctx.scout_trajectory, strict_selectors=True)
        assert synthesized is not None
        label = workflow_update_module._SYNTHESIZED_BLOCK_LABEL
        submitted_code = (
            textwrap.dedent(synthesized.code).rstrip()
            + f'\n{label}_output = {{"premium": "$123", "eligible": True}}\nreturn {label}_output\n'
        )
        submitted = _yaml(
            f"""
            title: Quote
            workflow_definition:
              blocks:
              - block_type: code
                label: {label}
                code: |
{textwrap.indent(submitted_code, " " * 18)}
            """
        )
        schema = (
            '{"type":"object","properties":{"premium":{"type":"string"},'
            '"eligible":{"type":"boolean"}},"required":["premium","eligible"]}'
        )
        metadata = _terminal_metadata(label, "quote result")
        metadata["claimed_outcomes"][0]["goal_value_paths"] = ["premium", "eligible"]
        metadata["claimed_outcomes"][0]["extraction_schema"] = schema
        metadata["claimed_outcomes"][0]["extraction_schema_provenance"] = "self_authored"
        metadata["terminal_verifier_expectations"][0]["goal_value_paths"] = ["premium", "eligible"]
        metadata["terminal_verifier_expectations"][0]["extraction_schema"] = schema
        metadata["terminal_verifier_expectations"][0]["extraction_schema_provenance"] = "self_authored"

        result = await _update_workflow({"workflow_yaml": submitted, "code_artifact_metadata": [metadata]}, ctx)

        assert result["ok"] is True
        assert result["data"]["imposed_substitutions"]["preserved_extraction_suffix"] is True
        assert "scrubbed_stale_selected_goal_value_paths" not in result["data"]["imposed_substitutions"]
        artifact = ctx.code_artifact_metadata[label]
        assert artifact["claimed_outcomes"][0]["goal_value_paths"] == ["premium", "eligible"]
        assert artifact["terminal_verifier_expectations"][0]["goal_value_paths"] == ["premium", "eligible"]
        assert artifact["claimed_outcomes"][0]["extraction_schema"] == schema
        parsed = parse_workflow_yaml(ctx.workflow_yaml)
        assert isinstance(parsed, dict)
        code_blocks = [block for block in workflow_blocks(parsed) if block.get("block_type") == "code"]
        assert len(code_blocks) > 1
        code = str(_code_blocks(parsed)[label]["code"])
        assert f"return {label}_output" in code
        assert all(label + "_output" not in str(block.get("code") or "") for block in code_blocks[:-1])

    def test_rejects_selected_extraction_suffix_page_mutation(self) -> None:
        ctx = _quote_ctx()
        synthesized = workflow_update_module.synthesize_code_block(ctx.scout_trajectory, strict_selectors=True)
        assert synthesized is not None
        submitted_code = textwrap.dedent(synthesized.code).rstrip() + '\nawait page.goto("https://example.com/other")\n'
        submitted = _yaml(
            f"""
            title: Quote
            workflow_definition:
              blocks:
              - block_type: code
                label: {workflow_update_module._SYNTHESIZED_BLOCK_LABEL}
                code: |
{textwrap.indent(submitted_code, " " * 18)}
            """
        )

        result = workflow_update_module._maybe_impose_synthesized_code_block(submitted, ctx)

        assert any(
            "extraction suffix contains unscouted browser action" in violation for violation in result.violations
        )
        assert result.substitutions is None

    @pytest.mark.asyncio
    async def test_p10_shaped_stale_metadata_imposes_scout_spine(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _quote_ctx()
        submitted = _yaml(
            """
            title: Quote
            workflow_definition:
              blocks:
              - block_type: code
                label: quote_flow
                code: |
                  await page.locator("#zip").fill(str(zip_code))
                  await page.locator("#continue").click()
                  await page.locator("#electricDate").fill("2026-07-01")
                  await page.locator("#electricPlan").select_option("basic")
                  await page.locator("#coverage-next").click()
            """
        )

        result = await _update_workflow(
            {
                "workflow_yaml": submitted,
                "code_artifact_metadata": [_terminal_metadata("quote_flow", "quote result")],
            },
            ctx,
        )

        assert result["ok"] is True
        parsed = parse_workflow_yaml(ctx.workflow_yaml)
        assert isinstance(parsed, dict)
        code = str(_single_code_block(parsed)["code"])
        assert "#electricDate" not in code
        assert "#electricPlan" not in code
        assert code.index('page.locator("#zip")') < code.index('page.locator("#continue")')
        assert code.index('page.locator("#continue")') < code.index('page.locator("#coverage-next")')


class TestBareDropSupersession:
    def test_selector_refines_css_accepts_identity_qualifiers(self) -> None:
        for candidate in (
            'button[data-action="status"]',
            "button#submit",
            "button.primary",
            'button[aria-label="Close ]"]',  # a literal ] inside the attribute value must not read as a combinator
        ):
            assert workflow_update_module._selector_refines("button", candidate) is True

    def test_selector_refines_role_accepts_named_same_role(self) -> None:
        assert workflow_update_module._selector_refines("role=button", 'role=button[name="Next"]') is True

    def test_selector_refines_rejects_positional_structural_and_cross_shape(self) -> None:
        bare = "button"
        for candidate in (
            "button:nth-child(2)",
            "button:nth-of-type(2)",
            "button >> nth=1",
            "button.primary span",
            "button#x + button",
            "button[data-x] > svg",
            "button:visible",
            "button:enabled",
            "button:not(.foo)",
            'button:has-text("Next")',
            "a[href]",
            "buttonx[id=1]",
            "button",
        ):
            assert workflow_update_module._selector_refines(bare, candidate) is False
        assert workflow_update_module._selector_refines("role=button", 'role=link[name="Next"]') is False
        assert workflow_update_module._selector_refines("role=button", 'button[data-action="x"]') is False
        assert workflow_update_module._selector_refines("button", 'role=button[name="x"]') is False
        assert workflow_update_module._selector_refines("role=button", 'role=button[name="N"] >> nth=1') is False

    def test_stable_bare_click_refiner_accepts_only_same_kind_text_and_role_anchors(self) -> None:
        assert (
            _stable_same_kind_bare_click_refiner("button", "xpath=//button[normalize-space()='Check Order Status']")
            is True
        )
        assert _stable_same_kind_bare_click_refiner("button", 'role=button[name="Next"]') is True
        for candidate in (
            "xpath=//a[normalize-space()='Check Order Status']",
            'role=link[name="Next"]',
            "xpath=(//button[normalize-space()='Check Order Status'])[2]",
            "xpath=//button[contains(normalize-space(), 'Check')]",
        ):
            assert _stable_same_kind_bare_click_refiner("button", candidate) is False

    def test_supersession_true_returns_pairing_record(self) -> None:
        dropped = {
            "reason_code": "ambiguous_bare_selector",
            "tool_name": "click",
            "selector": "button",
            "trajectory_index": 1,
        }
        ctx = _resale_ctx()
        claimed: set[int] = set()
        forgiven, record = workflow_update_module._bare_drop_superseded_on_screen(
            dropped, ctx.scout_trajectory, claimed_refiner_indices=claimed
        )
        assert forgiven is True
        assert record == {
            "dropped_index": 1,
            "dropped_selector": "button",
            "refiner_index": 2,
            "refiner_selector": 'button[data-action="status"]',
            "source_url": _RESALE_URL,
        }
        assert claimed == {2}

    def test_supersession_false_across_different_source_url(self) -> None:
        ctx = _resale_ctx()
        ctx.scout_trajectory[2]["source_url"] = "https://example.com/other"
        dropped = {
            "reason_code": "ambiguous_bare_selector",
            "tool_name": "click",
            "selector": "button",
            "trajectory_index": 1,
        }
        forgiven, record = workflow_update_module._bare_drop_superseded_on_screen(
            dropped, ctx.scout_trajectory, claimed_refiner_indices=set()
        )
        assert forgiven is False
        assert record is None

    def test_supersession_false_without_later_refiner(self) -> None:
        ctx = _resale_ctx(refiner_selector="button:nth-of-type(2)")
        dropped = {
            "reason_code": "ambiguous_bare_selector",
            "tool_name": "click",
            "selector": "button",
            "trajectory_index": 1,
        }
        forgiven, _ = workflow_update_module._bare_drop_superseded_on_screen(
            dropped, ctx.scout_trajectory, claimed_refiner_indices=set()
        )
        assert forgiven is False

    def test_supersession_false_on_empty_source_url(self) -> None:
        ctx = _resale_ctx()
        ctx.scout_trajectory[1]["source_url"] = ""
        dropped = {
            "reason_code": "ambiguous_bare_selector",
            "tool_name": "click",
            "selector": "button",
            "trajectory_index": 1,
        }
        forgiven, _ = workflow_update_module._bare_drop_superseded_on_screen(
            dropped, ctx.scout_trajectory, claimed_refiner_indices=set()
        )
        assert forgiven is False

    def test_supersession_false_on_out_of_bounds_index(self) -> None:
        ctx = _resale_ctx()
        for bad_index in (-1, 99, "1", None):
            dropped = {
                "reason_code": "ambiguous_bare_selector",
                "tool_name": "click",
                "selector": "button",
                "trajectory_index": bad_index,
            }
            forgiven, _ = workflow_update_module._bare_drop_superseded_on_screen(
                dropped, ctx.scout_trajectory, claimed_refiner_indices=set()
            )
            assert forgiven is False

    def test_imposition_forgives_mid_trajectory_bare_drop_and_records_substitution(self) -> None:
        ctx = _resale_ctx()
        result = workflow_update_module._maybe_impose_synthesized_code_block(_resale_submitted_yaml(), ctx)

        assert result.violations == []
        assert result.substitutions is not None
        forgiven = result.substitutions["forgiven_superseded_bare_drops"]
        assert forgiven == [
            {
                "dropped_index": 1,
                "dropped_selector": "button",
                "refiner_index": 2,
                "refiner_selector": 'button[data-action="status"]',
                "source_url": _RESALE_URL,
            }
        ]
        block = _single_code_block(parse_workflow_yaml(result.workflow_yaml))
        assert 'button[data-action=\\"status\\"]' in str(block["code"])

    def test_imposition_keeps_bare_drop_fatal_when_sibling_is_positional(self) -> None:
        ctx = _resale_ctx(refiner_selector="button:nth-of-type(2)")
        result = workflow_update_module._maybe_impose_synthesized_code_block(
            _resale_submitted_yaml("button:nth-of-type(2)"), ctx
        )

        assert any("ambiguous_bare_selector" in violation for violation in result.violations)
        assert result.substitutions is None

    def test_imposition_one_refiner_does_not_forgive_two_bare_drops(self) -> None:
        ctx = _resale_ctx()
        ctx.scout_trajectory = [
            {"tool_name": "click", "selector": "#start", "source_url": _RESALE_URL, "trajectory_index": 0},
            {"tool_name": "click", "selector": "button", "source_url": _RESALE_URL, "trajectory_index": 1},
            {"tool_name": "click", "selector": "button", "source_url": _RESALE_URL, "trajectory_index": 2},
            {
                "tool_name": "click",
                "selector": 'button[data-action="status"]',
                "source_url": _RESALE_URL,
                "trajectory_index": 3,
            },
        ]
        submitted = _yaml(
            """
            title: Order status
            workflow_definition:
              blocks:
              - block_type: code
                label: order_status
                code: |
                  await page.locator("#start").click()
                  await page.locator("button[data-action=\\"status\\"]").click()
            """
        )
        result = workflow_update_module._maybe_impose_synthesized_code_block(submitted, ctx)

        assert any("ambiguous_bare_selector" in violation for violation in result.violations)

    def test_imposition_two_refiners_forgive_two_bare_drops(self) -> None:
        ctx = _resale_ctx()
        ctx.scout_trajectory = [
            {"tool_name": "click", "selector": "#start", "source_url": _RESALE_URL, "trajectory_index": 0},
            {"tool_name": "click", "selector": "button", "source_url": _RESALE_URL, "trajectory_index": 1},
            {"tool_name": "click", "selector": "button", "source_url": _RESALE_URL, "trajectory_index": 2},
            {
                "tool_name": "click",
                "selector": 'button[data-action="open"]',
                "source_url": _RESALE_URL,
                "trajectory_index": 3,
            },
            {
                "tool_name": "click",
                "selector": 'button[data-action="status"]',
                "source_url": _RESALE_URL,
                "trajectory_index": 4,
            },
        ]
        submitted = _yaml(
            """
            title: Order status
            workflow_definition:
              blocks:
              - block_type: code
                label: order_status
                code: |
                  await page.locator("#start").click()
                  await page.locator("button[data-action=\\"open\\"]").click()
                  await page.locator("button[data-action=\\"status\\"]").click()
            """
        )
        result = workflow_update_module._maybe_impose_synthesized_code_block(submitted, ctx)

        assert result.violations == []
        assert result.substitutions is not None
        forgiven = result.substitutions["forgiven_superseded_bare_drops"]
        assert {record["dropped_index"] for record in forgiven} == {1, 2}
        assert {record["refiner_index"] for record in forgiven} == {3, 4}

    def test_imposition_two_exact_text_xpath_button_refiners_forgive_two_bare_drops(self) -> None:
        ctx = _resale_ctx()
        first_refiner = "xpath=//button[normalize-space()='Check Order Status']"
        second_refiner = "xpath=//button[normalize-space()='View / Download']"
        ctx.scout_trajectory = [
            {
                "tool_name": "type_text",
                "selector": "#order-id",
                "source_url": _RESALE_URL,
                "typed_length": 6,
                "typed_value": "abc123",
                "trajectory_index": 0,
            },
            {"tool_name": "click", "selector": "button", "source_url": _RESALE_URL, "trajectory_index": 1},
            {"tool_name": "click", "selector": "button", "source_url": _RESALE_URL, "trajectory_index": 2},
            {"tool_name": "click", "selector": first_refiner, "source_url": _RESALE_URL, "trajectory_index": 3},
            {"tool_name": "click", "selector": second_refiner, "source_url": _RESALE_URL, "trajectory_index": 4},
        ]
        submitted = _yaml(
            """
            title: Order status
            workflow_definition:
              blocks:
              - block_type: code
                label: order_status
                code: |
                  await page.locator("#order-id").fill(str(order_id))
                  await page.locator("xpath=//button[normalize-space()='Check Order Status']").click()
                  await page.locator("xpath=//button[normalize-space()='View / Download']").click()
            """
        )

        result = workflow_update_module._maybe_impose_synthesized_code_block(submitted, ctx)

        assert result.violations == []
        assert result.substitutions is not None
        forgiven = result.substitutions["forgiven_superseded_bare_drops"]
        assert {record["dropped_index"] for record in forgiven} == {1, 2}
        assert {record["refiner_index"] for record in forgiven} == {3, 4}
        assert {record["refiner_selector"] for record in forgiven} == {first_refiner, second_refiner}
        block = _single_code_block(parse_workflow_yaml(result.workflow_yaml))
        assert first_refiner in str(block["code"])
        assert second_refiner in str(block["code"])

    def test_imposition_one_exact_text_xpath_refiner_does_not_forgive_two_bare_drops(self) -> None:
        ctx = _resale_ctx()
        refiner = "xpath=//button[normalize-space()='Check Order Status']"
        ctx.scout_trajectory = [
            {"tool_name": "click", "selector": "#start", "source_url": _RESALE_URL, "trajectory_index": 0},
            {"tool_name": "click", "selector": "button", "source_url": _RESALE_URL, "trajectory_index": 1},
            {"tool_name": "click", "selector": "button", "source_url": _RESALE_URL, "trajectory_index": 2},
            {"tool_name": "click", "selector": refiner, "source_url": _RESALE_URL, "trajectory_index": 3},
        ]
        submitted = _yaml(
            """
            title: Order status
            workflow_definition:
              blocks:
              - block_type: code
                label: order_status
                code: |
                  await page.locator("#start").click()
                  await page.locator("xpath=//button[normalize-space()='Check Order Status']").click()
            """
        )

        result = workflow_update_module._maybe_impose_synthesized_code_block(submitted, ctx)

        assert any("ambiguous_bare_selector" in violation for violation in result.violations)

    def test_imposition_positional_xpath_refiner_keeps_bare_drop_fatal(self) -> None:
        ctx = _resale_ctx()
        positional_refiner = "xpath=(//button[normalize-space()='Check Order Status'])[2]"
        ctx.scout_trajectory = [
            {"tool_name": "click", "selector": "#start", "source_url": _RESALE_URL, "trajectory_index": 0},
            {"tool_name": "click", "selector": "button", "source_url": _RESALE_URL, "trajectory_index": 1},
            {"tool_name": "click", "selector": positional_refiner, "source_url": _RESALE_URL, "trajectory_index": 2},
        ]
        submitted = _yaml(
            """
            title: Order status
            workflow_definition:
              blocks:
              - block_type: code
                label: order_status
                code: |
                  await page.locator("#start").click()
                  await page.locator("xpath=(//button[normalize-space()='Check Order Status'])[2]").click()
            """
        )

        result = workflow_update_module._maybe_impose_synthesized_code_block(submitted, ctx)

        assert any("ambiguous_bare_selector" in violation for violation in result.violations)
        assert result.substitutions is None

    @pytest.mark.asyncio
    async def test_auto_act_non_navigating_reads_role_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx = _auto_act_scout_ctx()
        captured: dict[str, object] = {}

        async def _fake_resolve(
            _ctx: AgentContext, selector: str | None, *, allow_browser_read: bool
        ) -> tuple[str, str]:
            captured["allow_browser_read"] = allow_browser_read
            return "button", "Continue"

        monkeypatch.setattr(scouting_module, "_resolve_scout_role_name", _fake_resolve)

        async def _same_url(_ctx: AgentContext) -> str:
            return "https://example.com/orders"

        monkeypatch.setattr(scouting_module, "_live_working_page_url", _same_url)

        async def _evidence(_ctx: AgentContext, *, url: str) -> dict[str, object] | None:
            return None

        monkeypatch.setattr(scouting_module, "_scout_act_observe_page_evidence", _evidence)

        acted = await scouting_module._auto_act_on_repeat(
            ctx,
            {"data": {}},
            url="https://example.com/orders",
            target={"selector": "#continue", "text": "Continue"},
        )

        assert acted is True
        assert captured["allow_browser_read"] is True
        last = ctx.scout_trajectory[-1]
        assert last["role"] == "button"
        assert last["accessible_name"] == "Continue"

    @pytest.mark.asyncio
    async def test_auto_act_navigating_skips_browser_read(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx = _auto_act_scout_ctx()
        captured: dict[str, object] = {}

        async def _fake_resolve(
            _ctx: AgentContext, selector: str | None, *, allow_browser_read: bool
        ) -> tuple[str, str]:
            captured["allow_browser_read"] = allow_browser_read
            return "", ""

        monkeypatch.setattr(scouting_module, "_resolve_scout_role_name", _fake_resolve)

        urls = iter(["https://example.com/orders", "https://example.com/status"])

        async def _moving_url(_ctx: AgentContext) -> str:
            return next(urls)

        monkeypatch.setattr(scouting_module, "_live_working_page_url", _moving_url)

        async def _evidence(_ctx: AgentContext, *, url: str) -> dict[str, object] | None:
            return None

        monkeypatch.setattr(scouting_module, "_scout_act_observe_page_evidence", _evidence)

        acted = await scouting_module._auto_act_on_repeat(
            ctx,
            {"data": {}},
            url="https://example.com/orders",
            target={"selector": "#continue", "text": "Continue"},
        )

        assert acted is True
        assert captured["allow_browser_read"] is False


def _auto_act_scout_ctx() -> AgentContext:
    ctx = AgentContext.__new__(AgentContext)
    ctx.browser_session_id = None
    ctx.scouted_interactions = []
    ctx.scout_trajectory = []
    ctx.scout_observed_terminal_criterion_ids = set()
    ctx.completion_criteria_turn_state = None
    ctx.discovery_mcp_server = _AutoActClickServer()
    return ctx


class _AutoActClickServer:
    async def call_internal_tool(self, tool_name: str, args: dict[str, object]) -> dict[str, object]:
        return {"ok": True, "data": {"selector": args.get("selector")}}


def _declaration_stamp_ctx() -> CopilotContext:
    ctx = _code_only_ctx()
    ctx.turn_id = "t-decl"
    ctx.scout_trajectory = []
    ctx.request_policy = RequestPolicy(
        completion_criteria=[
            CompletionCriterion(
                id="c_record",
                outcome="The returned record includes record id.",
                output_path="output.record_id",
            ),
            CompletionCriterion(
                id="c_blocker",
                outcome="A blocker is reported when the site blocks submission.",
                contingent_on="the site blocks submission",
                contingent_antecedent_output_path="output.blocker",
            ),
        ]
    )
    return ctx


def _degraded_declaration_stamp_ctx() -> CopilotContext:
    ctx = _code_only_ctx()
    ctx.turn_id = "t-degraded-decl"
    ctx.scout_trajectory = []
    requested_paths = (
        "output.confirmation_number",
        "output.account_number",
        "output.start_date",
        "output.deposit_amount",
        "output.next_owner",
    )
    ctx.request_policy = RequestPolicy(
        completion_criteria=[
            *[
                CompletionCriterion(
                    id=f"slot_{index}",
                    outcome=f"The returned record includes {path}.",
                    request_slot_id=f"slot_{index}",
                    pinability="unpinnable",
                    mint_disposition="degraded",
                    mint_degrade="undecidable_judgment",
                    requested_output_floor_rekeyed=True,
                    floor_rekeyed_from_path=path,
                )
                for index, path in enumerate(requested_paths)
            ],
            CompletionCriterion(
                id="c_blocker",
                outcome="A blocker is reported when the site blocks submission.",
                contingent_on="the site blocks submission",
                contingent_antecedent_output_path="output.blocker",
            ),
        ]
    )
    return ctx


class TestDeclarationContractStamp:
    @pytest.mark.parametrize("empty_value", ["None", "''", "'   '", "[]", "{}", "[None]", "[{}]", "{'a': None}"])
    def test_static_empty_observation_value_is_non_run_eligible(self, empty_value: str) -> None:
        ctx = _declaration_stamp_ctx()
        workflow_yaml = _collapsed_spine_yaml(f'return {{"output": {{"record_id": {empty_value}, "blocker": None}}}}')
        metadata = [
            workflow_update_module._metadata_contract_template(
                block_label="extract_record",
                required_paths={"output.record_id"},
                source="requested_output_contract",
                reason_code="requested_output_contract_missing_output_coverage",
                declaration_paths={"output.blocker"},
            )
        ]

        evaluation = workflow_update_module._evaluate_output_contract_for_code_block(
            ctx,
            workflow_yaml,
            metadata,
            allow_static_return_advisory=True,
            enforce_value_bearing_liveness=True,
        )

        assert evaluation is not None
        assert evaluation.missing_metadata_paths == []
        assert evaluation.missing_schema_paths == []
        assert evaluation.missing_return_paths == []
        assert evaluation.payload["reason_code"] == "value_bearing_output_required"
        assert evaluation.can_attempt_run is False

    @pytest.mark.parametrize("value", ["0", "False", "'record-123'", "[None, 'x']", "{'a': 'x'}"])
    def test_static_value_bearing_observation_remains_run_eligible(self, value: str) -> None:
        ctx = _declaration_stamp_ctx()
        workflow_yaml = _collapsed_spine_yaml(f'return {{"output": {{"record_id": {value}, "blocker": None}}}}')
        metadata = [
            workflow_update_module._metadata_contract_template(
                block_label="extract_record",
                required_paths={"output.record_id"},
                source="requested_output_contract",
                reason_code="requested_output_contract_missing_output_coverage",
                declaration_paths={"output.blocker"},
            )
        ]

        evaluation = workflow_update_module._evaluate_output_contract_for_code_block(
            ctx, workflow_yaml, metadata, enforce_value_bearing_liveness=True
        )

        assert evaluation is not None
        assert evaluation.has_deficiencies is False

    def test_full_metadata_advisory_rejects_declaration_only_return(self) -> None:
        ctx = _declaration_stamp_ctx()
        required_paths = {"output.record_id", "output.blocker"}
        signature = workflow_update_module._stable_output_contract_key(
            workflow_update_module._output_contract_scope_key(ctx), required_paths
        )
        workflow_update_module._grant_output_contract_advisory_run(ctx, signature)
        workflow_yaml = _collapsed_spine_yaml('await page.click("#submit")\nreturn {"output": {"blocker": None}}')
        metadata = [
            workflow_update_module._metadata_contract_template(
                block_label="extract_record",
                required_paths={"output.record_id"},
                source="requested_output_contract",
                reason_code="requested_output_contract_missing_output_coverage",
                declaration_paths={"output.blocker"},
            )
        ]

        evaluation = workflow_update_module._evaluate_output_contract_for_code_block(
            ctx,
            workflow_yaml,
            metadata,
            allow_static_return_advisory=True,
            enforce_value_bearing_liveness=True,
        )

        assert evaluation is not None
        assert evaluation.missing_metadata_paths == []
        assert evaluation.missing_schema_paths == []
        assert evaluation.missing_return_paths == ["output.record_id"]
        assert evaluation.payload["static_return_advisory_paths"] == []
        assert evaluation.payload["reason_code"] == "value_bearing_output_required"
        assert evaluation.can_attempt_run is False

    def test_degraded_requested_slots_make_declaration_only_contract_non_run_eligible(self) -> None:
        ctx = _degraded_declaration_stamp_ctx()
        workflow_yaml = _collapsed_spine_yaml('return {"output": {"blocker": None}}')

        formed_yaml, metadata, applied = workflow_update_module._impose_output_contract_envelope_after_steering(
            ctx, workflow_yaml, []
        )
        evaluation = workflow_update_module._evaluate_output_contract_for_code_block(
            ctx,
            formed_yaml,
            metadata,
            allow_static_return_advisory=True,
            enforce_value_bearing_liveness=True,
        )

        assert applied is False
        assert formed_yaml == workflow_yaml
        assert evaluation is not None
        signature = evaluation.canonical_signature
        assert ctx.output_contract_armed_directive_fingerprint_by_signature.get(signature) == (
            workflow_update_module._VALUE_BEARING_PREARM_FINGERPRINT_PREFIX
            + workflow_update_module._output_contract_structural_fingerprint(workflow_yaml, signature)
        )
        assert evaluation.payload["contract_liveness"] == "degraded_empty"
        assert evaluation.payload["reason_code"] == "value_bearing_output_required"
        assert evaluation.can_attempt_run is False
        assert "canonical_evaluation_paths" not in evaluation.payload
        assert [row["request_slot_id"] for row in evaluation.payload["degraded_request_slots"]] == [
            "slot_0",
            "slot_1",
            "slot_2",
            "slot_3",
            "slot_4",
        ]
        assert [row["floor_rekeyed_from_path"] for row in evaluation.payload["degraded_request_slots"]] == [
            "output.confirmation_number",
            "output.account_number",
            "output.start_date",
            "output.deposit_amount",
            "output.next_owner",
        ]

    def test_degraded_declaration_only_directive_arm_is_idempotent(self) -> None:
        ctx = _degraded_declaration_stamp_ctx()
        workflow_yaml = _collapsed_spine_yaml('return {"output": {"blocker": None}}')

        first_yaml, _, first_applied = workflow_update_module._impose_output_contract_envelope_after_steering(
            ctx, workflow_yaml, []
        )
        second_yaml, _, second_applied = workflow_update_module._impose_output_contract_envelope_after_steering(
            ctx, first_yaml, []
        )

        assert first_applied is False
        assert second_applied is False
        assert first_yaml == workflow_yaml
        assert second_yaml == workflow_yaml
        assert len(ctx.output_contract_armed_directive_fingerprint_by_signature) == 1

    def test_mixed_value_and_declaration_return_remains_valid(self) -> None:
        ctx = _declaration_stamp_ctx()
        workflow_yaml = _collapsed_spine_yaml(
            'record_id = await page.locator("#record-id").text_content()\nreturn {"output": {"record_id": record_id}}'
        )

        formed_yaml, metadata, applied = workflow_update_module._impose_output_contract_envelope_after_steering(
            ctx, workflow_yaml, []
        )
        evaluation = workflow_update_module._evaluate_output_contract_for_code_block(ctx, formed_yaml, metadata)

        assert applied is True
        assert evaluation is not None
        assert evaluation.has_deficiencies is False
        code = str(workflow_blocks(parse_workflow_yaml(formed_yaml))[0].get("code") or "")
        assert '"record_id": record_id' in code
        assert '"blocker": None' in code

    def test_formation_does_not_create_declaration_only_scaffold(self) -> None:
        ctx = _declaration_stamp_ctx()
        workflow_yaml = _collapsed_spine_yaml('await page.click("#submit")\nreturn {}')

        new_yaml, _metadata, applied = workflow_update_module._impose_output_contract_envelope_after_steering(
            ctx, workflow_yaml, []
        )

        assert applied is True
        assert new_yaml == workflow_yaml
        code = str(workflow_blocks(parse_workflow_yaml(new_yaml))[0].get("code") or "")
        assert "blocker" not in code
        evaluation = workflow_update_module._evaluate_output_contract_for_code_block(ctx, new_yaml, _metadata)
        assert evaluation is not None
        assert evaluation.can_attempt_run is False

    def test_stamp_is_idempotent_across_calls(self) -> None:
        ctx = _declaration_stamp_ctx()
        workflow_yaml = _collapsed_spine_yaml('await page.click("#submit")')

        first_yaml, _metadata, _applied = workflow_update_module._impose_output_contract_envelope_after_steering(
            ctx, workflow_yaml, []
        )
        again_yaml, _metadata2, _applied2 = workflow_update_module._impose_output_contract_envelope_after_steering(
            ctx, first_yaml, []
        )

        assert again_yaml == first_yaml

    def test_stamp_skips_without_single_owner_block(self) -> None:
        ctx = _declaration_stamp_ctx()
        workflow_yaml = _yaml(
            "title: Two blocks\n"
            "workflow_definition:\n"
            "  blocks:\n"
            "  - block_type: code\n"
            "    label: first\n"
            "    code: |\n"
            '      await page.click("#a")\n'
            "  - block_type: code\n"
            "    label: second\n"
            "    code: |\n"
            '      await page.click("#b")\n'
        )

        new_yaml, _metadata, applied = workflow_update_module._impose_output_contract_envelope_after_steering(
            ctx, workflow_yaml, []
        )

        assert applied is False
        assert new_yaml == workflow_yaml

    def test_stamp_refused_for_statically_empty_observation_code(self) -> None:
        ctx = _declaration_stamp_ctx()
        workflow_yaml = _collapsed_spine_yaml('return {"output": {"record_id": None}}')
        contract = workflow_update_module._output_contract_required_paths_source(ctx)

        stamped_yaml, applied = workflow_update_module._stamp_declaration_contract_defaults(
            ctx, workflow_yaml, [], contract, "sig-declaration-stamp"
        )

        assert applied is False
        assert stamped_yaml == workflow_yaml

    def test_stamp_applies_for_runtime_populated_observation_code(self) -> None:
        ctx = _declaration_stamp_ctx()
        workflow_yaml = _collapsed_spine_yaml(
            'record_id = await page.inner_text("#record")\nreturn {"output": {"record_id": record_id}}'
        )
        contract = workflow_update_module._output_contract_required_paths_source(ctx)

        stamped_yaml, applied = workflow_update_module._stamp_declaration_contract_defaults(
            ctx, workflow_yaml, [], contract, "sig-declaration-stamp"
        )

        assert applied is True
        code = str(workflow_blocks(parse_workflow_yaml(stamped_yaml))[0].get("code") or "")
        assert '"blocker": None' in code


def test_static_return_synthesis_refuses_statically_none_observation_local() -> None:
    code = "record_id = None"

    keyed, violations = workflow_update_module._extraction_code_with_value_bearing_static_return(
        code, required_paths={"output.record_id"}, declaration_paths={"output.blocker"}
    )

    assert violations
    assert keyed == code


def test_static_return_synthesis_accepts_runtime_populated_observation_local() -> None:
    code = 'record_id = await page.inner_text("#record")'

    keyed, violations = workflow_update_module._extraction_code_with_value_bearing_static_return(
        code, required_paths={"output.record_id"}, declaration_paths={"output.blocker"}
    )

    assert violations == []
    assert {"output.record_id", "output.blocker"} <= workflow_update_module._code_block_produced_output_paths(keyed)


def test_static_return_synthesis_refuses_covered_statically_empty_return() -> None:
    code = 'return {"output": {"record_id": None, "blocker": None}}'

    _, violations = workflow_update_module._extraction_code_with_value_bearing_static_return(
        code, required_paths={"output.record_id"}, declaration_paths={"output.blocker"}
    )

    assert violations


def test_value_bearing_lattice_unpack_after_empty_literal_is_fail_open() -> None:
    code = 'extra = await collect()\nreturn {"output": {"record_id": None, **extra}}'

    assert workflow_update_module._statically_lacks_value_bearing_observation_paths(code, {"output.record_id"}) is False


def test_value_bearing_lattice_top_level_unpack_after_output_key_is_fail_open() -> None:
    code = 'extra = await collect()\nreturn {"output": {"record_id": None}, **extra}'

    assert workflow_update_module._statically_lacks_value_bearing_observation_paths(code, {"output.record_id"}) is False


def test_value_bearing_lattice_empty_literal_after_unpack_stays_refused() -> None:
    code = 'extra = await collect()\nreturn {"output": {**extra, "record_id": None}}'

    assert workflow_update_module._statically_lacks_value_bearing_observation_paths(code, {"output.record_id"}) is True


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ('runtime_value = await collect()\nreturn {"output": runtime_value}', "proven"),
        (
            'runtime_value = await collect()\npayload = {"output": runtime_value}\nalias = payload\nreturn alias',
            "proven",
        ),
        ("runtime_value = await collect()\nreturn runtime_value", "unknown"),
        ('runtime_value = await collect()\nreturn {"request_id": runtime_value}', "absent"),
        ('runtime_value = await collect()\nreturn {"output.request_id": runtime_value}', "absent"),
        ('runtime_value = await collect()\noutput_key = "output"\nreturn {output_key: runtime_value}', "unknown"),
        (
            'runtime_value = await collect()\nextra = await collect_extra()\nreturn {"output": runtime_value, **extra}',
            "unknown",
        ),
        (
            'if should_return_output:\n    return {"output": runtime_value}\nreturn {"request_id": runtime_value}',
            "absent",
        ),
        ('return payload\npayload = {"output": runtime_value}', "unknown"),
        ("await page.click('#submit')", "absent"),
    ],
)
def test_root_output_envelope_state_requires_literal_root_on_every_return(code: str, expected: str) -> None:
    assert workflow_update_module._root_output_envelope_state(code) == expected


def _saved_code_workflow(
    code: str, label: str = "finalize_service", extra: dict[str, str] | None = None
) -> SimpleNamespace:
    blocks = [{"label": label, "block_type": "code", "code": code}]
    blocks.extend(
        {"label": extra_label, "block_type": "code", "code": extra_code}
        for extra_label, extra_code in (extra or {}).items()
    )
    labels = {str(block["label"]) for block in blocks}
    return SimpleNamespace(
        workflow_id="w",
        workflow_definition={"parameters": [], "blocks": blocks},
        get_output_parameter=lambda requested: SimpleNamespace(label=requested) if requested in labels else None,
    )


class TestValueBearingDispatchPreflight:
    @staticmethod
    def _guarded_app(monkeypatch: pytest.MonkeyPatch, *, organization: None | EllipsisType = ...) -> AsyncMock:
        if organization is ...:
            get_organization = AsyncMock(side_effect=AssertionError("org lookup called"))
        else:
            get_organization = AsyncMock(return_value=organization)
        prepare_workflow = AsyncMock(side_effect=AssertionError("prepare_workflow called"))
        monkeypatch.setattr(
            run_execution_module.app,
            "DATABASE",
            SimpleNamespace(organizations=SimpleNamespace(get_organization=get_organization)),
        )
        monkeypatch.setattr(
            run_execution_module.app,
            "WORKFLOW_SERVICE",
            SimpleNamespace(prepare_workflow=prepare_workflow),
        )
        return prepare_workflow

    @pytest.mark.asyncio
    async def test_dispatch_of_mixed_contract_workflow_passes_preflight(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx = _declaration_stamp_ctx()
        ctx.staged_workflow = _saved_code_workflow(
            'record_id = await page.locator("#record-id").text_content()\n'
            'return {"output": {"record_id": record_id, "blocker": None}}'
        )
        self._guarded_app(monkeypatch, organization=None)

        result = await run_execution_module._run_blocks_and_collect_debug(
            {"block_labels": ["finalize_service"], "parameters": {}}, ctx
        )

        assert result == {"ok": False, "error": "Organization not found"}

    @pytest.mark.asyncio
    async def test_dispatch_without_output_contract_passes_preflight_for_value_bearing_code(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _code_only_ctx()
        ctx.staged_workflow = _saved_code_workflow(
            'record_id = await page.locator("#record-id").text_content()\nreturn {"output": {"record_id": record_id}}'
        )
        self._guarded_app(monkeypatch, organization=None)

        result = await run_execution_module._run_blocks_and_collect_debug(
            {"block_labels": ["finalize_service"], "parameters": {}}, ctx
        )

        assert result == {"ok": False, "error": "Organization not found"}

    @pytest.mark.asyncio
    async def test_owner_excluded_subset_run_dispatches_when_saved_workflow_bears_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _declaration_stamp_ctx()
        ctx.staged_workflow = _saved_code_workflow(
            'return {"logged_in": True}',
            label="login",
            extra={
                "extract_record": (
                    'record_id = await page.locator("#record-id").text_content()\n'
                    'return {"output": {"record_id": record_id, "blocker": None}}'
                )
            },
        )
        self._guarded_app(monkeypatch, organization=None)

        result = await run_execution_module._run_blocks_and_collect_debug(
            {"block_labels": ["login"], "parameters": {}}, ctx
        )

        assert result == {"ok": False, "error": "Organization not found"}

    @pytest.mark.asyncio
    async def test_solo_empty_setup_block_dispatches_in_value_bearing_workflow(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _code_only_ctx()
        ctx.request_policy = RequestPolicy(completion_criteria=[])
        ctx.staged_workflow = _saved_code_workflow(
            "return {}",
            label="setup",
            extra={"extract_record": 'value = await fetch()\nreturn {"output": {"record_id": value}}'},
        )
        self._guarded_app(monkeypatch, organization=None)

        result = await run_execution_module._run_blocks_and_collect_debug(
            {"block_labels": ["setup"], "parameters": {}}, ctx
        )

        assert result == {"ok": False, "error": "Organization not found"}


def _gate_blocks(sibling_code: str) -> list[dict[str, object]]:
    return [
        {"block_type": "code", "label": "sibling_stage", "code": sibling_code},
        {"block_type": "code", "label": "extract_record", "code": 'return {"output": {}}\n'},
    ]


def _gate_validation(sibling_code: str, diagnostics: SynthesisDiagnostics, synthesized_code: str | None = None):
    blocks = _gate_blocks(sibling_code)
    return workflow_update_module._whole_trajectory_browser_surface_violations(
        code_blocks=blocks,
        selected_code_block=blocks[1],
        submitted_selected_code=str(blocks[1]["code"]),
        synthesized_code=synthesized_code if synthesized_code is not None else _SPINE_SYNTH_CODE,
        synthesized_diagnostics=diagnostics,
    )


class TestBrowserSurfaceRejectionProvenance:
    def test_never_captured_mutation_rejected_with_rescout_move(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            workflow_update_module,
            "synthesize_code_block",
            lambda *a, **k: _fake_spine_synthesized(diagnostics=_spine_emission_diagnostics()),
        )
        ctx = _imposition_split_ctx()

        with capture_logs() as logs:
            result = workflow_update_module._maybe_impose_synthesized_code_block(
                _already_split_spine_yaml(extra_sibling_code='await page.locator("#hallucinated").click()'), ctx
            )

        assert any("unscouted browser action" in violation for violation in result.violations)
        assert any(
            "never_captured" in violation and "re-scout that step" in violation for violation in result.violations
        )
        events = [log for log in logs if log["event"] == "copilot_browser_surface_rejection_provenance"]
        assert len(events) == 1
        assert events[0]["kind"] == "never_captured"
        assert events[0]["site"] == "whole_trajectory"
        assert "#hallucinated" in events[0]["action"]

    def test_same_receiver_divergent_call_shape_is_shape_diverged_with_nearest(self) -> None:
        validation = _gate_validation(
            'await page.locator("#stage-a").click(timeout=5000)\n', _spine_emission_diagnostics()
        )

        assert len(validation.provenance) == 1
        provenance = validation.provenance[0]
        assert provenance.kind == "shape_diverged"
        assert provenance.divergence_source == "synthesized"
        assert provenance.nearest_receiver == "page.locator('#stage-a')"
        assert provenance.nearest_method == "click"
        assert provenance.nearest_selector == "#stage-a"
        assert any(
            "shape_diverged (synthesized)" in violation and "captured selector '#stage-a'" in violation
            for violation in validation.violations
        )

    def test_trajectory_dropped_rung_is_not_never_captured(self) -> None:
        diagnostics = _spine_emission_diagnostics()
        diagnostics.dropped_interactions.append(
            {"trajectory_index": 2, "tool_name": "click", "selector": "#gone", "reason_code": "ambiguous_bare_selector"}
        )

        validation = _gate_validation('await page.locator("#gone").click()\n', diagnostics)

        assert len(validation.provenance) == 1
        provenance = validation.provenance[0]
        assert provenance.kind == "shape_diverged"
        assert provenance.divergence_source == "trajectory_dropped"
        assert provenance.nearest_selector == "#gone"
        assert not any("never_captured" in violation for violation in validation.violations)

    def test_unaccounted_branch_drop_does_not_relabel_an_unrelated_mutation(self) -> None:
        diagnostics = _spine_emission_diagnostics()
        diagnostics.dropped_interactions.append(
            {"trajectory_index": 2, "tool_name": "click", "selector": "#skipped", "reason_code": "unaccounted_branch"}
        )

        validation = _gate_validation('await page.locator("#hallucinated").click()\n', diagnostics)

        assert [provenance.kind for provenance in validation.provenance] == ["never_captured"]

    def test_locator_form_divergence_matches_emitted_record(self) -> None:
        diagnostics = SynthesisDiagnostics(
            emitted_interaction_count=1,
            emitted_interactions=[
                {
                    "trajectory_index": 0,
                    "tool_name": "click",
                    "method": "click",
                    "selector": "#go",
                    "locator": 'page.get_by_role("button", name="Go")',
                }
            ],
        )

        validation = _gate_validation(
            'await page.locator("#go").click()\n',
            diagnostics,
            synthesized_code='await page.get_by_role("button", name="Go").click()',
        )

        assert len(validation.provenance) == 1
        provenance = validation.provenance[0]
        assert provenance.kind == "shape_diverged"
        assert provenance.divergence_source == "synthesized"
        assert provenance.nearest_receiver == 'page.get_by_role("button", name="Go")'
        assert provenance.nearest_selector == "#go"
        assert not any("never_captured" in violation for violation in validation.violations)

    def test_ambiguous_alias_mutation_names_rewrite_move(self) -> None:
        validation = _gate_validation(
            'do_click = page.locator("#x").click\nawait do_click()\n', _spine_emission_diagnostics()
        )

        assert len(validation.provenance) == 1
        provenance = validation.provenance[0]
        assert provenance.kind == "ambiguous"
        assert provenance.nearest_method is None
        assert provenance.nearest_receiver is None
        assert provenance.nearest_selector is None
        assert provenance.divergence_source is None
        assert any(
            "ambiguous browser action" in violation and "rewrite it as a direct page/locator call" in violation
            for violation in validation.violations
        )

    def test_extraction_suffix_unscouted_mutation_carries_provenance(self) -> None:
        ctx = _quote_ctx()
        synthesized = workflow_update_module.synthesize_code_block(ctx.scout_trajectory, strict_selectors=True)
        assert synthesized is not None
        submitted_code = (
            textwrap.dedent(synthesized.code).rstrip() + '\nawait page.locator("#electricDate").fill("2026-07-01")\n'
        )
        submitted = _yaml(
            f"""
            title: Quote
            workflow_definition:
              blocks:
              - block_type: code
                label: {workflow_update_module._SYNTHESIZED_BLOCK_LABEL}
                code: |
{textwrap.indent(submitted_code, " " * 18)}
            """
        )

        with capture_logs() as logs:
            result = workflow_update_module._maybe_impose_synthesized_code_block(submitted, ctx)

        assert any(
            "extraction suffix contains unscouted browser action" in violation for violation in result.violations
        )
        events = [log for log in logs if log["event"] == "copilot_browser_surface_rejection_provenance"]
        assert len(events) == 1
        assert events[0]["site"] == "extraction_suffix"
        assert events[0]["kind"] == "never_captured"

    def test_extraction_suffix_exact_duplicate_is_suffix_disallowed(self) -> None:
        ctx = _quote_ctx()
        synthesized = workflow_update_module.synthesize_code_block(ctx.scout_trajectory, strict_selectors=True)
        assert synthesized is not None
        submitted_code = textwrap.dedent(synthesized.code).rstrip() + '\nawait page.locator("#coverage-next").click()\n'
        submitted = _yaml(
            f"""
            title: Quote
            workflow_definition:
              blocks:
              - block_type: code
                label: {workflow_update_module._SYNTHESIZED_BLOCK_LABEL}
                code: |
{textwrap.indent(submitted_code, " " * 18)}
            """
        )

        with capture_logs() as logs:
            result = workflow_update_module._maybe_impose_synthesized_code_block(submitted, ctx)

        assert any(
            "suffix_disallowed" in violation and "remove the duplicate" in violation for violation in result.violations
        )
        events = [log for log in logs if log["event"] == "copilot_browser_surface_rejection_provenance"]
        assert len(events) == 1
        assert events[0]["kind"] == "suffix_disallowed"
        assert events[0]["divergence_source"] is None

    def test_never_captured_still_rejects_with_empty_diagnostics(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(workflow_update_module, "synthesize_code_block", lambda *a, **k: _fake_spine_synthesized())
        ctx = _imposition_split_ctx()

        result = workflow_update_module._maybe_impose_synthesized_code_block(
            _already_split_spine_yaml(extra_sibling_code='await page.locator("#hallucinated").click()'), ctx
        )

        assert any("unscouted browser action" in violation for violation in result.violations)
        assert any("never_captured" in violation for violation in result.violations)
        assert result.substitutions is None


class TestSeparatedSpineFastPathRecord:
    def test_set_equality_pass_unchanged_and_duplicate_loss_recorded(self) -> None:
        blocks = [
            {"block_type": "code", "label": "s1", "code": 'await page.locator("#a").click()\n'},
            {"block_type": "code", "label": "s2", "code": 'await page.locator("#b").click()\n'},
            {"block_type": "code", "label": "extract", "code": 'return {"output": {}}\n'},
        ]
        synthesized_code = (
            'await page.locator("#a").click()\nawait page.locator("#b").click()\nawait page.locator("#a").click()'
        )

        with capture_logs() as logs:
            already_imposed = workflow_update_module._separated_spine_already_imposed(
                blocks, blocks[2], synthesized_code
            )

        assert already_imposed is True
        events = [log for log in logs if log["event"] == "copilot_separated_spine_fast_path"]
        assert len(events) == 1
        assert events[0]["spine_coverage"] == "set_equality"
        assert events[0]["synthesized_mutation_count"] == 3
        assert events[0]["sibling_signature_count"] == 2
        assert events[0]["duplicate_rungs_lost"] is True


class TestImpositionSkippedAfterUpdateRecord:
    def test_post_update_early_return_emits_skip_record(self) -> None:
        ctx = _imposition_split_ctx()
        ctx.update_workflow_called = True
        workflow_yaml = _already_split_spine_yaml()

        with capture_logs() as logs:
            result = workflow_update_module._maybe_impose_synthesized_code_block(workflow_yaml, ctx)

        assert result.violations == []
        assert result.workflow_yaml == workflow_yaml
        events = [log for log in logs if log["event"] == "copilot_imposition_skipped_after_update"]
        assert len(events) == 1
        assert events[0]["trajectory_length"] == len(ctx.scout_trajectory)
        assert events[0]["reopen_download_target"] is False
        assert events[0]["reopen_persistence_after_failed_run"] is False
        assert events[0]["reopen_author_time_reject"] is False
        assert events[0]["reaches_goal"] is False

    def test_reach_admitted_attempt_names_its_own_lane(self) -> None:
        ctx = _reaching_extraction_ctx()
        ctx.flow_evidence = []
        ctx.update_workflow_called = True

        with capture_logs() as logs:
            workflow_update_module._maybe_impose_synthesized_code_block(_live_read_submitted_yaml(), ctx)

        events = [log for log in logs if log["event"] == "copilot_imposition_admitted_after_update"]
        assert len(events) == 1
        assert events[0]["admission_key"] == "goal_reaching_spine_unlanded"
        assert events[0]["goal_complete"] is False


class TestScoutCaptureParityAccounting:
    def test_unresolvable_selector_bail_emits_capture_loss(self) -> None:
        ctx = _code_only_ctx()
        before = list(ctx.scouted_interactions)

        with capture_logs() as logs:
            scouting_module._record_scouted_interaction(
                ctx, tool_name="click", selector="", source_url="https://example.com/step"
            )

        assert ctx.scouted_interactions == before
        events = [log for log in logs if log["event"] == "copilot_scout_capture_loss"]
        assert len(events) == 1
        assert events[0]["tool_name"] == "click"
        assert events[0]["reason"] == "unresolvable_selector"
        assert events[0]["url"] == "https://example.com/step"

    def test_cap_eviction_emits_per_collection_records(self) -> None:
        ctx = _code_only_ctx()
        ctx.scouted_interactions = []
        ctx.scout_trajectory = []
        for index in range(scouting_module._MAX_SCOUTED_INTERACTIONS):
            scouting_module._record_scouted_interaction(
                ctx, tool_name="click", selector=f"#item-{index}", source_url="https://example.com/list"
            )

        with capture_logs() as logs:
            scouting_module._record_scouted_interaction(
                ctx, tool_name="click", selector="#item-overflow", source_url="https://example.com/list"
            )

        events = [log for log in logs if log["event"] == "copilot_scout_interaction_evicted"]
        by_collection = {event["collection"]: event for event in events}
        assert set(by_collection) == {"scout_trajectory", "scouted_interactions"}
        assert by_collection["scout_trajectory"]["trajectory_index"] == 0
        assert "trajectory_index" not in by_collection["scouted_interactions"]
        assert by_collection["scouted_interactions"]["selector"] == "#item-0"
        assert len(ctx.scouted_interactions) == scouting_module._MAX_SCOUTED_INTERACTIONS
        assert len(ctx.scout_trajectory) == scouting_module._MAX_SCOUTED_INTERACTIONS
        assert ctx.scouted_interactions[0]["selector"] == "#item-1"

    def test_trajectory_index_stays_monotonic_across_evictions(self) -> None:
        ctx = _code_only_ctx()
        ctx.scouted_interactions = []
        ctx.scout_trajectory = []
        for index in range(scouting_module._MAX_SCOUTED_INTERACTIONS + 2):
            scouting_module._record_scouted_interaction(
                ctx, tool_name="click", selector=f"#item-{index}", source_url="https://example.com/list"
            )

        indexes = [item["trajectory_index"] for item in ctx.scout_trajectory]
        assert indexes == list(range(2, scouting_module._MAX_SCOUTED_INTERACTIONS + 2))

    def test_dedup_replacement_is_not_an_eviction(self) -> None:
        ctx = _code_only_ctx()
        ctx.scouted_interactions = []
        ctx.scout_trajectory = []
        for index in range(scouting_module._MAX_SCOUTED_INTERACTIONS):
            scouting_module._record_scouted_interaction(
                ctx, tool_name="click", selector=f"#item-{index}", source_url="https://example.com/list"
            )

        with capture_logs() as logs:
            scouting_module._record_scouted_interaction(
                ctx, tool_name="click", selector="#item-5", source_url="https://example.com/list"
            )

        events = [log for log in logs if log["event"] == "copilot_scout_interaction_evicted"]
        assert all(event["collection"] == "scout_trajectory" for event in events)
        assert len(ctx.scouted_interactions) == scouting_module._MAX_SCOUTED_INTERACTIONS

    @pytest.mark.asyncio
    async def test_fill_carry_rebind_eviction_goes_through_shared_accounting(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(scouting_module, "_fill_carry_validation_failure", AsyncMock(return_value=None))
        ctx = _code_only_ctx()
        ctx.scout_trajectory = [
            {"tool_name": "click", "selector": f"#item-{index}", "trajectory_index": index}
            for index in range(scouting_module._MAX_SCOUTED_INTERACTIONS)
        ]
        ctx.prior_fill_carry = [
            FillCarry(
                tool_name="type_text",
                selector="#carried",
                source_url="https://example.com/form",
                typed_value="abc",
            ).model_dump()
        ]

        with capture_logs() as logs:
            await scouting_module._maybe_rebind_prior_fill_carry(
                ctx, page_evidence={"current_url": "https://example.com/form"}, url="https://example.com/form"
            )

        events = [log for log in logs if log["event"] == "copilot_scout_interaction_evicted"]
        assert len(events) == 1
        assert events[0]["collection"] == "scout_trajectory"
        assert events[0]["trajectory_index"] == 0
        assert len(ctx.scout_trajectory) == scouting_module._MAX_SCOUTED_INTERACTIONS
        assert ctx.scout_trajectory[-1]["selector"] == "#carried"
        assert ctx.scout_trajectory[-1]["carried"] is True


def _under_build_draft_yaml() -> str:
    return _yaml(
        f"""
        title: Entry lookup
        workflow_definition:
          blocks:
          - block_type: code
            label: {workflow_update_module._SYNTHESIZED_BLOCK_LABEL}
            code: |
              await page.locator("#stage-a").click()
        """
    )


def _drifted_spine_synthesized(diagnostics: SynthesisDiagnostics | None = None) -> SynthesizedCodeBlock:
    return _fake_spine_synthesized(
        code='await page.locator("#stage-a").click()',
        diagnostics=diagnostics if diagnostics is not None else _spine_emission_diagnostics(),
    )


class TestScoutedSpineUnderBuild:
    def test_browser_surface_mutations_are_source_ordered_across_nesting(self) -> None:
        # A rung nested inside an `if` appears earlier in source than a later top-level rung.
        # ast.walk enumerates breadth-first, so without a source-order sort the nested call would
        # be reported after the top-level one and the ordered-subsequence coverage scan would
        # falsely flag a present rung as uncovered.
        code = textwrap.dedent(
            """
            page = ctx.page
            if ctx.needs_consent:
                page.get_by_role("button", name="Alpha").click()
            page.get_by_role("link", name="Bravo").click()
            """
        )
        direct_mutations, _unscouted, _ambiguous = workflow_update_module._browser_surface_for_code(code)
        shapes = [mutation.call_shape for mutation in direct_mutations]
        alpha_index = next(i for i, shape in enumerate(shapes) if "Alpha" in shape)
        bravo_index = next(i for i, shape in enumerate(shapes) if "Bravo" in shape)
        assert alpha_index < bravo_index

    def test_under_build_draft_rejected_with_pass_route(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            workflow_update_module, "synthesize_code_block", lambda *a, **k: _drifted_spine_synthesized()
        )
        ctx = _quote_ctx()

        with capture_logs() as logs:
            result = workflow_update_module._maybe_impose_synthesized_code_block(_under_build_draft_yaml(), ctx)

        assert any("scouted_spine_under_build" in violation for violation in result.violations)
        assert any("#stage-b" in violation for violation in result.violations)
        assert any("remaining synthesized rungs" in violation for violation in result.violations)
        assert all("fill_credential_field" not in violation for violation in result.violations)
        assert result.repair_context is not None
        assert result.repair_context.reason_code == "scouted_spine_under_build"
        events = [log for log in logs if log["event"] == "copilot_scouted_spine_under_build"]
        assert len(events) == 1
        assert events[0]["required_rung_count"] == 2
        assert events[0]["covered_rung_count"] == 1

    def test_retained_index_in_no_lane_still_declines_imposition(self, monkeypatch: pytest.MonkeyPatch) -> None:
        diagnostics = _spine_emission_diagnostics()
        diagnostics.retained_trajectory_indices = [0, 1, 2]
        monkeypatch.setattr(
            workflow_update_module,
            "synthesize_code_block",
            lambda *a, **k: _fake_spine_synthesized(diagnostics=diagnostics),
        )
        ctx = _quote_ctx()

        with capture_logs() as logs:
            result = workflow_update_module._maybe_impose_synthesized_code_block(
                _records_block_yaml(_SPINE_SYNTH_CODE), ctx
            )

        assert result.substitutions is None
        assert any("scouted_spine_unrecorded_index" in violation for violation in result.violations)
        assert result.repair_context is not None
        assert result.repair_context.reason_code == "scouted_spine_unrecorded_index"
        events = [log for log in logs if log["event"] == "copilot_scouted_spine_under_build"]
        assert [event["reason_code"] for event in events] == ["scouted_spine_unrecorded_index"]

    def test_unaccounted_branch_drop_still_declines_imposition(self, monkeypatch: pytest.MonkeyPatch) -> None:
        diagnostics = _spine_emission_diagnostics()
        diagnostics.retained_trajectory_indices = [0, 1, 2]
        diagnostics.dropped_interactions.append(
            {"trajectory_index": 2, "tool_name": "click", "selector": "#gone", "reason_code": "unaccounted_branch"}
        )
        monkeypatch.setattr(
            workflow_update_module,
            "synthesize_code_block",
            lambda *a, **k: _fake_spine_synthesized(diagnostics=diagnostics),
        )
        ctx = _quote_ctx()

        result = workflow_update_module._maybe_impose_synthesized_code_block(
            _records_block_yaml(_SPINE_SYNTH_CODE), ctx
        )

        assert result.substitutions is None
        assert any("unaccounted_branch" in violation for violation in result.violations)

    def test_lane_flagged_emissions_do_not_trigger_under_build(self, monkeypatch: pytest.MonkeyPatch) -> None:
        diagnostics = SynthesisDiagnostics(
            emitted_interaction_count=4,
            emitted_interactions=[
                {
                    "trajectory_index": 0,
                    "tool_name": "click",
                    "method": "click",
                    "selector": "#stage-a",
                    "locator": 'page.locator("#stage-a")',
                },
                {
                    "trajectory_index": 1,
                    "tool_name": "click",
                    "method": "click",
                    "selector": "#dismiss",
                    "locator": 'page.locator("#dismiss")',
                    "lane": "optional_dismissal",
                },
                {
                    "trajectory_index": 2,
                    "tool_name": "type_text",
                    "method": "input_value",
                    "selector": "#readonly-field",
                    "locator": 'page.locator("#readonly-field")',
                    "lane": "readonly_skip",
                },
                {
                    "trajectory_index": 3,
                    "tool_name": "click",
                    "method": "click",
                    "selector": "#opener",
                    "locator": 'page.locator("#opener")',
                    "lane": "entry_recovery",
                },
            ],
        )
        monkeypatch.setattr(
            workflow_update_module,
            "synthesize_code_block",
            lambda *a, **k: _drifted_spine_synthesized(diagnostics),
        )
        ctx = _quote_ctx()

        with capture_logs() as logs:
            result = workflow_update_module._maybe_impose_synthesized_code_block(_under_build_draft_yaml(), ctx)

        assert result.violations == []
        assert result.substitutions is not None
        assert not [log for log in logs if log["event"] == "copilot_scouted_spine_under_build"]

    def test_forgiven_prefix_interactions_do_not_trigger_under_build(self, monkeypatch: pytest.MonkeyPatch) -> None:
        diagnostics = SynthesisDiagnostics(
            emitted_interaction_count=1,
            emitted_interactions=[
                {
                    "trajectory_index": 1,
                    "tool_name": "click",
                    "method": "click",
                    "selector": "#stage-a",
                    "locator": 'page.locator("#stage-a")',
                }
            ],
            forgiven_interactions=[{"trajectory_index": 0, "tool_name": "click", "lane": "entry_replay_prefix"}],
        )
        monkeypatch.setattr(
            workflow_update_module,
            "synthesize_code_block",
            lambda *a, **k: _drifted_spine_synthesized(diagnostics),
        )
        ctx = _quote_ctx()

        with capture_logs() as logs:
            result = workflow_update_module._maybe_impose_synthesized_code_block(_under_build_draft_yaml(), ctx)

        assert result.violations == []
        assert not [log for log in logs if log["event"] == "copilot_scouted_spine_under_build"]

    def test_full_spine_draft_from_real_generator_does_not_fire(self) -> None:
        ctx = _quote_ctx()
        synthesized = workflow_update_module.synthesize_code_block(ctx.scout_trajectory, strict_selectors=True)
        assert synthesized is not None
        submitted = _yaml(
            f"""
            title: Quote
            workflow_definition:
              blocks:
              - block_type: code
                label: {workflow_update_module._SYNTHESIZED_BLOCK_LABEL}
                code: |
{textwrap.indent(textwrap.dedent(synthesized.code).strip(), " " * 18)}
            """
        )

        with capture_logs() as logs:
            result = workflow_update_module._maybe_impose_synthesized_code_block(submitted, ctx)

        assert result.violations == []
        assert not [log for log in logs if log["event"] == "copilot_scouted_spine_under_build"]


def _records_spine_ctx() -> CopilotContext:
    ctx = _code_only_ctx()
    _enable_imposition(ctx)
    ctx.scout_trajectory = [
        {
            "tool_name": "click",
            "selector": "#stage-a",
            "source_url": "https://example.com/records",
            "trajectory_index": 0,
        },
        {
            "tool_name": "click",
            "selector": "#stage-b",
            "source_url": "https://example.com/records",
            "trajectory_index": 1,
        },
        {
            "tool_name": "click",
            "selector": "#stage-c",
            "source_url": "https://example.com/records",
            "trajectory_index": 2,
        },
    ]
    return ctx


def _records_block_yaml(code_body: str) -> str:
    indented = textwrap.indent(textwrap.dedent(code_body).strip(), " " * 14)
    return _yaml(
        f"""
        title: Records
        workflow_definition:
          blocks:
          - block_type: code
            label: {workflow_update_module._SYNTHESIZED_BLOCK_LABEL}
            code: |
{indented}
        """
    )


def _checkpoint_eligible_ctx() -> CopilotContext:
    ctx = _records_spine_ctx()
    ctx.update_workflow_called = True
    ctx.persisted_draft_browser_calls = [("click", 'page.locator("#stage-a")')]
    return ctx


def _credential_spine_ctx() -> CopilotContext:
    ctx = _code_only_ctx()
    _enable_imposition(ctx)
    ctx.update_workflow_called = True
    ctx.persisted_draft_browser_calls = [("click", 'page.locator("#stage-a")')]
    ctx.scout_trajectory = [
        {
            "tool_name": "click",
            "selector": "#stage-a",
            "source_url": "https://example.com/records",
            "trajectory_index": 0,
        },
        _credential_fill_interaction(
            "username", credential_id="cred_records", source_url="https://example.com/records"
        ),
        _credential_fill_interaction(
            "password", credential_id="cred_records", source_url="https://example.com/records"
        ),
    ]
    return ctx


def _credential_spine_block_yaml(synthesized: SynthesizedCodeBlock) -> str:
    credential_parameter = next(
        parameter for parameter in synthesized.parameters if str(parameter.get("credential_id") or "")
    )
    indented = textwrap.indent(textwrap.dedent(synthesized.code).strip(), " " * 14)
    return _yaml(
        f"""
        title: Records
        workflow_definition:
          parameters:
          - parameter_type: workflow
            workflow_parameter_type: credential_id
            key: {credential_parameter["key"]}
            default_value: {credential_parameter["credential_id"]}
          blocks:
          - block_type: code
            label: {workflow_update_module._SYNTHESIZED_BLOCK_LABEL}
            parameter_keys:
            - {credential_parameter["key"]}
            code: |
{indented}
        """
    )


class TestScoutedSpinePersistSeamCoverage:
    def test_separated_split_branch_under_coverage_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        diagnostics = _spine_emission_diagnostics()
        diagnostics.emitted_interactions.append(
            {
                "trajectory_index": 2,
                "tool_name": "click",
                "method": "click",
                "selector": "#stage-c",
                "locator": 'page.locator("#stage-c")',
                "call_source": 'await page.locator("#stage-c").click()',
            }
        )
        monkeypatch.setattr(
            workflow_update_module,
            "synthesize_code_block",
            lambda *a, **k: _fake_spine_synthesized(diagnostics=diagnostics),
        )
        ctx = _imposition_split_ctx()
        workflow_yaml = _records_block_yaml(
            _SPINE_SYNTH_CODE
            + '\nvalue = await page.locator("#result").inner_text()\nreturn {"output": {"record_id": value}}'
        )

        with capture_logs() as logs:
            result = workflow_update_module._maybe_impose_synthesized_code_block(workflow_yaml, ctx)

        assert any("scouted_spine_under_build" in violation for violation in result.violations)
        assert any('await page.locator("#stage-c").click()' in violation for violation in result.violations)
        events = [log for log in logs if log["event"] == "copilot_scouted_spine_under_build"]
        assert len(events) == 1
        assert events[0]["site"] == "separated_split"

    @pytest.mark.asyncio
    async def test_credential_scout_reject_carries_open_obligation_artifact(self) -> None:
        ctx = _credential_spine_ctx()
        synthesized = workflow_update_module.synthesize_code_block(ctx.scout_trajectory, strict_selectors=True)
        assert synthesized is not None

        result = await _update_workflow({"workflow_yaml": _credential_spine_block_yaml(synthesized)}, ctx)

        assert result["ok"] is False
        assert result["data"]["failure_type"] == "missing_credential_or_init"
        assert "later submit action on the same page" in result["error"]
        assert "The persisted draft is missing scouted rung(s)." in result["error"]
        assert "Missing rung source to reuse verbatim" in result["error"]
        assert ".password" in result["error"]


class TestPartitionAwareImpositionFastPath:
    def _synthesize(self, trajectory: list[dict[str, object]]) -> SynthesizedCodeBlock:
        result = workflow_update_module.synthesize_code_block(trajectory, strict_selectors=True)
        assert result is not None
        return result

    def _covering_blocks(self, synthesized: SynthesizedCodeBlock) -> list[dict[str, object]]:
        return [{"code": synthesized.interaction_code or synthesized.code}]

    def test_clean_covered_draft_allows_early_return(self) -> None:
        trajectory = [
            {"tool_name": "click", "selector": "#stage-a", "source_url": "https://example.com/records"},
            {"tool_name": "click", "selector": "#stage-b"},
        ]
        synthesized = self._synthesize(trajectory)
        assert (
            workflow_update_module._draft_leaves_scouted_partition_open(
                self._covering_blocks(synthesized), synthesized=synthesized, scout_trajectory=trajectory
            )
            is False
        )

    def test_covered_but_dropped_unforgiven_blocks_early_return(self) -> None:
        trajectory = [
            {"tool_name": "click", "selector": "#stage-a", "source_url": "https://example.com/records"},
            {"tool_name": "press_key", "key": ""},
            {"tool_name": "click", "selector": "#stage-b"},
        ]
        synthesized = self._synthesize(trajectory)
        assert (
            workflow_update_module._draft_leaves_scouted_partition_open(
                self._covering_blocks(synthesized), synthesized=synthesized, scout_trajectory=trajectory
            )
            is True
        )

    def test_covered_but_truncated_blocks_early_return(self) -> None:
        trajectory = [
            {"tool_name": "click", "selector": f"#stage-{index}", "source_url": "https://example.com/records"}
            for index in range(_MAX_STEPS + 2)
        ]
        synthesized = self._synthesize(trajectory)
        assert synthesized.diagnostics.truncated is True
        assert (
            workflow_update_module._draft_leaves_scouted_partition_open(
                self._covering_blocks(synthesized), synthesized=synthesized, scout_trajectory=trajectory
            )
            is True
        )


class TestFinalYamlStructuralGate:
    def test_dangling_next_block_label_is_flagged(self) -> None:
        parsed = {
            "workflow_definition": {
                "blocks": [
                    {"block_type": "code", "label": "a", "next_block_label": "missing"},
                    {"block_type": "code", "label": "b"},
                ]
            }
        }
        violation = workflow_update_module._dangling_next_block_label_violation(parsed)
        assert violation is not None
        assert "missing" in violation

    def test_resolved_next_block_label_passes(self) -> None:
        parsed = {
            "workflow_definition": {
                "blocks": [
                    {"block_type": "code", "label": "a", "next_block_label": "b"},
                    {"block_type": "code", "label": "b"},
                ]
            }
        }
        assert workflow_update_module._dangling_next_block_label_violation(parsed) is None


def _full_coverage_calls() -> list[tuple[str, str]]:
    return [
        ("click", 'page.locator("#stage-a")'),
        ("click", 'page.locator("#stage-b")'),
        ("click", 'page.locator("#stage-c")'),
    ]


class TestScoutedSpineTurnHaltExit:
    def test_turn_halt_with_open_obligation_emits_unresolved(self) -> None:
        ctx = _checkpoint_eligible_ctx()
        halt = TurnHalt(
            kind=TurnHaltKind.LOOP_DETECTED,
            verdict=TurnHaltVerdict.BLOCKED,
        )

        with capture_logs() as logs:
            agent_module._build_turn_halt_exit_result(ctx, global_llm_context=None, halt=halt)

        unresolved = [log for log in logs if log["event"] == "copilot_scouted_spine_under_build_unresolved"]
        assert len(unresolved) == 1
        assert unresolved[0]["site"] == "turn_halt"


class TestAmbiguousRejectCarriesOpenObligationArtifact:
    def test_ambiguous_sibling_reject_with_open_obligation_carries_call_source(self) -> None:
        ctx = _checkpoint_eligible_ctx()
        record_build_test_outcome(ctx, _author_time_reject_outcome("metadata_reject"))
        submitted = _yaml(
            f"""
            title: Records
            workflow_definition:
              blocks:
              - block_type: code
                label: {workflow_update_module._SYNTHESIZED_BLOCK_LABEL}
                code: |
                  await page.locator("#stage-a").click()
              - block_type: code
                label: helper_stage
                code: |
                  opener = page.locator("#stage-b").click
                  await opener()
            """
        )

        result = workflow_update_module._maybe_impose_synthesized_code_block(submitted, ctx)

        assert any("ambiguous browser action" in violation for violation in result.violations)
        assert any('await page.locator("#stage-b").click()' in violation for violation in result.violations)
        assert any('await page.locator("#stage-c").click()' in violation for violation in result.violations)

    def test_ambiguous_reject_without_open_obligation_stays_bare(self) -> None:
        ctx = _checkpoint_eligible_ctx()
        ctx.persisted_draft_browser_calls = [
            ("click", 'page.locator("#stage-a")'),
            ("click", 'page.locator("#stage-b")'),
            ("click", 'page.locator("#stage-c")'),
        ]
        record_build_test_outcome(ctx, _author_time_reject_outcome("metadata_reject"))
        submitted = _yaml(
            f"""
            title: Records
            workflow_definition:
              blocks:
              - block_type: code
                label: {workflow_update_module._SYNTHESIZED_BLOCK_LABEL}
                code: |
                  await page.locator("#stage-a").click()
              - block_type: code
                label: helper_stage
                code: |
                  opener = page.locator("#stage-b").click
                  await opener()
            """
        )

        result = workflow_update_module._maybe_impose_synthesized_code_block(submitted, ctx)

        assert any("ambiguous browser action" in violation for violation in result.violations)
        assert not any("reuse verbatim" in violation for violation in result.violations)


class TestCredentialScoutGapMatcher:
    _PAGE_ONE = "https://portal.example.test/step-one"
    _PAGE_TWO = "https://portal.example.test/step-two"

    @staticmethod
    def _fill(credential_id: str, field: str, source_url: str) -> dict[str, object]:
        return {
            "tool_name": "fill_credential_field",
            "credential_id": credential_id,
            "credential_field": field,
            "selector": f"#{field}",
            "source_url": source_url,
        }

    @staticmethod
    def _click(source_url: str) -> dict[str, object]:
        return {"tool_name": "click", "selector": "input[type='submit']", "source_url": source_url}

    def test_missing_fields_reported_sorted_per_requirement(self) -> None:
        gap = credential_scout_gap(
            [self._fill("cred_a", "username", self._PAGE_ONE)],
            [(frozenset({"cred_a"}), frozenset({"username", "password"}))],
            requires_submit=False,
        )
        assert gap == ScoutGap(missing_fields=["password"], missing_submit=False)

    def test_no_matched_fill_means_missing_submit(self) -> None:
        gap = credential_scout_gap(
            [self._click(self._PAGE_ONE)],
            [(frozenset({"cred_a"}), frozenset({"username"}))],
            requires_submit=True,
        )
        assert gap == ScoutGap(missing_fields=["username"], missing_submit=True)

    def test_cross_requirement_accumulation_accepts_submit_on_either_matched_page(self) -> None:
        trajectory = [
            self._fill("cred_a", "username", self._PAGE_ONE),
            self._fill("cred_b", "password", self._PAGE_TWO),
            self._click(self._PAGE_ONE),
        ]
        gap = credential_scout_gap(
            trajectory,
            [
                (frozenset({"cred_a"}), frozenset({"username"})),
                (frozenset({"cred_b"}), frozenset({"password"})),
            ],
            requires_submit=True,
        )
        assert gap == ScoutGap(missing_fields=[], missing_submit=False)

    def test_submit_on_unmatched_page_stays_missing(self) -> None:
        trajectory = [
            self._fill("cred_a", "username", self._PAGE_ONE),
            self._click("https://portal.example.test/elsewhere"),
        ]
        gap = credential_scout_gap(
            trajectory,
            [(frozenset({"cred_a"}), frozenset({"username"}))],
            requires_submit=True,
        )
        assert gap == ScoutGap(missing_fields=[], missing_submit=True)

    def test_submit_before_latest_fill_stays_missing(self) -> None:
        trajectory = [
            self._click(self._PAGE_ONE),
            self._fill("cred_a", "username", self._PAGE_ONE),
        ]
        gap = credential_scout_gap(
            trajectory,
            [(frozenset({"cred_a"}), frozenset({"username"}))],
            requires_submit=True,
        )
        assert gap == ScoutGap(missing_fields=[], missing_submit=True)

    def test_sourceless_fill_accepts_any_later_submit(self) -> None:
        trajectory = [
            self._fill("cred_a", "username", ""),
            self._click("https://portal.example.test/elsewhere"),
        ]
        gap = credential_scout_gap(
            trajectory,
            [(frozenset({"cred_a"}), frozenset({"username"}))],
            requires_submit=True,
        )
        assert gap == ScoutGap(missing_fields=[], missing_submit=False)


class TestCredentialScoutGatePredicateCoherence:
    _TWO_FIELD_LOGIN_YAML = _credential_code_yaml(
        code="""
        await page.locator("#user").fill(login_credential.username)
        await page.locator("#pass").fill(login_credential.password)
        await page.locator("button[type='submit']").click()
        """,
        credential_id="cred_1",
    )

    @staticmethod
    def _trajectory(*steps: dict[str, object]) -> list[dict[str, object]]:
        return list(steps)

    def _gate_ctx(self, trajectory: list[dict[str, object]]) -> CopilotContext:
        ctx = _code_only_ctx()
        ctx.scout_trajectory = trajectory
        ctx.scouted_credential_field_inventory_by_credential_id = {"cred_1": frozenset({"username", "password"})}
        return ctx

    def test_predicate_complete_trajectory_passes_the_real_gate(self) -> None:
        helper = TestCredentialScoutGapMatcher
        trajectory = self._trajectory(
            helper._fill("cred_1", "username", helper._PAGE_ONE),
            helper._click(helper._PAGE_ONE),
            helper._fill("cred_1", "password", helper._PAGE_TWO),
            helper._click(helper._PAGE_TWO),
        )
        ctx = self._gate_ctx(trajectory)
        assert enforcement_module.synthesized_trajectory_is_goal_complete(ctx) is True
        assert workflow_update_module._credentialed_code_block_scout_gate_errors(self._TWO_FIELD_LOGIN_YAML, ctx) == []

    def test_predicate_incomplete_half_login_is_also_gate_rejected(self) -> None:
        helper = TestCredentialScoutGapMatcher
        trajectory = self._trajectory(
            helper._fill("cred_1", "username", helper._PAGE_ONE),
            helper._click(helper._PAGE_ONE),
        )
        ctx = self._gate_ctx(trajectory)
        assert enforcement_module.synthesized_trajectory_is_goal_complete(ctx) is False
        errors = workflow_update_module._credentialed_code_block_scout_gate_errors(self._TWO_FIELD_LOGIN_YAML, ctx)
        assert errors
        assert "password" in errors[0]


class TestTerminalActionScoutGate:
    _BUSINESS_URL = "https://portal.example.test/business/start-service"

    @staticmethod
    def _terminal_action_criterion(*, method_mandated: bool = False) -> CompletionCriterion:
        return CompletionCriterion(
            id="start_service_request",
            outcome="the business start-service request reaches its review page",
            kind="terminal_action",
            terminal_action_family="request",
            method_mandated=method_mandated,
        )

    def _login_prefix_ctx(self, *criteria: CompletionCriterion) -> CopilotContext:
        helper = TestCredentialScoutGapMatcher
        ctx = _code_only_ctx()
        ctx.scout_trajectory = [
            helper._fill("cred_1", "username", helper._PAGE_ONE),
            helper._click(helper._PAGE_ONE),
            helper._fill("cred_1", "password", helper._PAGE_TWO),
            helper._click(helper._PAGE_TWO),
        ]
        ctx.scouted_credential_field_inventory_by_credential_id = {"cred_1": frozenset({"username", "password"})}
        ctx.completion_criteria_turn_state = SimpleNamespace(decision=SimpleNamespace(criteria=tuple(criteria)))
        return ctx

    def _business_spine(self) -> list[dict[str, object]]:
        return [
            {
                "tool_name": "type_text",
                "selector": "#service-address",
                "source_url": self._BUSINESS_URL,
                "role": "textbox",
                "accessible_name": "Service Address",
                "trajectory_index": 4,
            },
            {
                "tool_name": "click",
                "selector": "#find-address",
                "source_url": self._BUSINESS_URL,
                "role": "button",
                "accessible_name": "Find Address",
                "trajectory_index": 5,
            },
        ]

    def test_login_prefix_with_unreached_terminal_action_is_not_goal_complete(self) -> None:
        ctx = self._login_prefix_ctx(self._terminal_action_criterion())
        assert enforcement_module.synthesized_trajectory_reaches_goal(ctx) is False
        assert enforcement_module.synthesized_trajectory_is_goal_complete(ctx) is False

    def test_login_is_the_whole_goal_stays_goal_complete(self) -> None:
        ctx = self._login_prefix_ctx()
        assert enforcement_module.synthesized_trajectory_is_goal_complete(ctx) is True

    def test_method_mandated_terminal_action_criterion_does_not_gate(self) -> None:
        ctx = self._login_prefix_ctx(self._terminal_action_criterion(method_mandated=True))
        assert enforcement_module.synthesized_trajectory_is_goal_complete(ctx) is True

    def test_observed_terminal_action_does_not_bypass_post_credential_floor(self) -> None:
        ctx = self._login_prefix_ctx(self._terminal_action_criterion())
        ctx.scout_observed_terminal_criterion_ids = {"start_service_request"}
        assert enforcement_module.synthesized_trajectory_is_goal_complete(ctx) is False

    def test_post_credential_business_spine_records_terminal_action_observation(self) -> None:
        ctx = self._login_prefix_ctx(self._terminal_action_criterion())
        ctx.scout_trajectory = list(ctx.scout_trajectory) + self._business_spine()
        assert enforcement_module.reached_terminal_action_criterion_ids(ctx) == {"start_service_request"}
        enforcement_module.record_reached_terminal_action_observation(ctx)
        assert ctx.scout_observed_terminal_criterion_ids == {"start_service_request"}
        assert enforcement_module.synthesized_trajectory_is_goal_complete(ctx) is True

    def test_post_login_open_then_submit_reaches_terminal_action(self) -> None:
        ctx = self._login_prefix_ctx(self._terminal_action_criterion())
        ctx.scout_trajectory = list(ctx.scout_trajectory) + [
            {"tool_name": "click", "selector": "#open-request", "trajectory_index": 4},
            {"tool_name": "click", "selector": "#submit-request", "trajectory_index": 5},
        ]

        assert enforcement_module.synthesized_trajectory_reaches_goal(ctx) is True
        assert enforcement_module.reached_terminal_action_criterion_ids(ctx) == {"start_service_request"}
        enforcement_module.record_reached_terminal_action_observation(ctx)
        assert enforcement_module.synthesized_trajectory_is_goal_complete(ctx) is True

    def test_post_login_three_click_business_spine_reaches_terminal_action(self) -> None:
        ctx = self._login_prefix_ctx(self._terminal_action_criterion())
        ctx.scout_trajectory = list(ctx.scout_trajectory) + [
            {"tool_name": "click", "selector": "#open-item", "trajectory_index": 4},
            {"tool_name": "click", "selector": "#add-to-cart", "trajectory_index": 5},
            {"tool_name": "click", "selector": "#place-order", "trajectory_index": 6},
        ]

        assert enforcement_module.synthesized_trajectory_reaches_goal(ctx) is True
        assert enforcement_module.reached_terminal_action_criterion_ids(ctx) == {"start_service_request"}

    def test_post_login_non_committing_click_sequence_stays_gated(self) -> None:
        ctx = self._login_prefix_ctx(self._terminal_action_criterion())
        ctx.scout_trajectory = list(ctx.scout_trajectory) + [
            {"tool_name": "click", "selector": "button", "role": "button", "trajectory_index": 4},
            {"tool_name": "click", "selector": "button", "role": "button", "trajectory_index": 5},
            {"tool_name": "click", "selector": "button", "role": "button", "trajectory_index": 6},
        ]

        assert enforcement_module.synthesized_trajectory_reaches_goal(ctx) is False
        assert enforcement_module.reached_terminal_action_criterion_ids(ctx) == set()

    def test_sourceless_enter_login_submit_establishes_business_boundary(self) -> None:
        ctx = self._login_prefix_ctx(self._terminal_action_criterion())
        ctx.scout_trajectory = [
            {
                "tool_name": "fill_credential_field",
                "credential_id": "cred_1",
                "credential_field": "username",
                "trajectory_index": 0,
            },
            {"tool_name": "press_key", "key": "Enter", "trajectory_index": 1},
            {"tool_name": "click", "selector": "#open-request", "trajectory_index": 2},
            {"tool_name": "click", "selector": "#submit-request", "trajectory_index": 3},
        ]
        ctx.scouted_credential_field_inventory_by_credential_id = {"cred_1": frozenset({"username"})}

        assert enforcement_module.synthesized_trajectory_reaches_goal(ctx) is True
        assert enforcement_module.reached_terminal_action_criterion_ids(ctx) == {"start_service_request"}

    def test_sourceless_non_enter_key_does_not_establish_login_boundary(self) -> None:
        ctx = self._login_prefix_ctx(self._terminal_action_criterion())
        ctx.scout_trajectory = [
            {
                "tool_name": "fill_credential_field",
                "credential_id": "cred_1",
                "credential_field": "username",
                "trajectory_index": 0,
            },
            {"tool_name": "press_key", "key": "Tab", "trajectory_index": 1},
            {"tool_name": "click", "selector": "#open-request", "trajectory_index": 2},
            {"tool_name": "click", "selector": "#submit-request", "trajectory_index": 3},
        ]
        ctx.scouted_credential_field_inventory_by_credential_id = {"cred_1": frozenset({"username"})}

        assert enforcement_module.synthesized_trajectory_reaches_goal(ctx) is False
        assert enforcement_module.reached_terminal_action_criterion_ids(ctx) == set()

    def test_post_login_boundary_ignores_divergent_sourced_click(self) -> None:
        login_url = "https://portal.example.test/login"
        ctx = self._login_prefix_ctx(self._terminal_action_criterion())
        ctx.scout_trajectory = [
            {
                "tool_name": "fill_credential_field",
                "credential_id": "cred_1",
                "credential_field": "username",
                "source_url": login_url,
                "trajectory_index": 0,
            },
            {
                "tool_name": "click",
                "accessible_name": "Learn more",
                "source_url": "https://portal.example.test/help",
                "trajectory_index": 1,
            },
            {
                "tool_name": "click",
                "selector": "button[type='submit']",
                "source_url": login_url,
                "trajectory_index": 2,
            },
            {
                "tool_name": "click",
                "selector": "#open-request",
                "source_url": self._BUSINESS_URL,
                "trajectory_index": 3,
            },
            {
                "tool_name": "click",
                "selector": "#submit-request",
                "source_url": self._BUSINESS_URL,
                "trajectory_index": 4,
            },
        ]
        ctx.scouted_credential_field_inventory_by_credential_id = {"cred_1": frozenset({"username"})}

        assert enforcement_module.synthesized_trajectory_reaches_goal(ctx) is True
        assert enforcement_module.reached_terminal_action_criterion_ids(ctx) == {"start_service_request"}

    def test_sourceless_divergent_click_uses_stable_login_submit_identity(self) -> None:
        ctx = self._login_prefix_ctx(self._terminal_action_criterion())
        ctx.scout_trajectory = [
            {
                "tool_name": "fill_credential_field",
                "credential_id": "cred_1",
                "credential_field": "username",
                "trajectory_index": 0,
            },
            {
                "tool_name": "click",
                "selector": "#login-help",
                "accessible_name": "Trouble signing in?",
                "trajectory_index": 1,
            },
            {"tool_name": "click", "accessible_name": "Sign in", "trajectory_index": 2},
            {"tool_name": "click", "selector": "#open-request", "trajectory_index": 3},
            {"tool_name": "click", "selector": "#submit-request", "trajectory_index": 4},
        ]
        ctx.scouted_credential_field_inventory_by_credential_id = {"cred_1": frozenset({"username"})}

        assert enforcement_module.synthesized_trajectory_reaches_goal(ctx) is True
        assert enforcement_module.reached_terminal_action_criterion_ids(ctx) == {"start_service_request"}

    def test_mixed_fill_sources_use_latest_sourceless_login_identity(self) -> None:
        helper = TestCredentialScoutGapMatcher
        ctx = self._login_prefix_ctx(self._terminal_action_criterion())
        ctx.scout_trajectory = [
            helper._fill("cred_1", "username", helper._PAGE_ONE),
            helper._fill("cred_1", "password", ""),
            {"tool_name": "click", "accessible_name": "Sign in", "trajectory_index": 2},
            {"tool_name": "click", "selector": "#open-request", "trajectory_index": 3},
            {"tool_name": "click", "selector": "#submit-request", "trajectory_index": 4},
        ]

        assert enforcement_module.synthesized_trajectory_reaches_goal(ctx) is True
        assert enforcement_module.reached_terminal_action_criterion_ids(ctx) == {"start_service_request"}
        enforcement_module.record_reached_terminal_action_observation(ctx)
        assert enforcement_module.synthesized_trajectory_is_goal_complete(ctx) is True

    def test_sourceless_login_submit_without_stable_identity_fails_closed(self) -> None:
        ctx = self._login_prefix_ctx(self._terminal_action_criterion())
        ctx.scout_trajectory = [
            {
                "tool_name": "fill_credential_field",
                "credential_id": "cred_1",
                "credential_field": "username",
                "trajectory_index": 0,
            },
            {"tool_name": "click", "accessible_name": "Help", "trajectory_index": 1},
            {"tool_name": "click", "selector": "button", "trajectory_index": 2},
            {"tool_name": "click", "selector": "#open-request", "trajectory_index": 3},
            {"tool_name": "click", "selector": "#submit-request", "trajectory_index": 4},
        ]
        ctx.scouted_credential_field_inventory_by_credential_id = {"cred_1": frozenset({"username"})}

        assert enforcement_module.synthesized_trajectory_reaches_goal(ctx) is False
        assert enforcement_module.reached_terminal_action_criterion_ids(ctx) == set()

    def test_login_only_trajectory_records_no_terminal_action_observation(self) -> None:
        ctx = self._login_prefix_ctx(self._terminal_action_criterion())
        enforcement_module.record_reached_terminal_action_observation(ctx)
        assert ctx.scout_observed_terminal_criterion_ids == set()

    def test_mfa_login_prefix_with_unreached_terminal_action_is_not_goal_complete(self) -> None:
        helper = TestCredentialScoutGapMatcher
        ctx = self._login_prefix_ctx(self._terminal_action_criterion())
        ctx.scout_trajectory = list(ctx.scout_trajectory) + [
            helper._fill("cred_1", "totp", helper._PAGE_TWO),
            helper._click(helper._PAGE_TWO),
        ]
        ctx.scouted_credential_field_inventory_by_credential_id = {
            "cred_1": frozenset({"username", "password", "totp"})
        }
        assert enforcement_module.reached_terminal_action_criterion_ids(ctx) == set()
        enforcement_module.record_reached_terminal_action_observation(ctx)
        assert ctx.scout_observed_terminal_criterion_ids == set()
        assert enforcement_module.synthesized_trajectory_is_goal_complete(ctx) is False


class TestCredentialScoutReopenSeam:
    @pytest.mark.asyncio
    async def test_pure_credential_reject_arms_then_same_identity_does_not_re_arm(self) -> None:
        ctx = _code_only_ctx()
        ctx.scout_trajectory = []
        yaml_text = TestCredentialScoutPersistGate._SUBMIT_CODE_YAML

        result = await _update_workflow({"workflow_yaml": yaml_text}, ctx)

        assert result["ok"] is False
        assert result["data"]["failure_type"] == "missing_credential_or_init"
        assert ctx.synthesized_block_reopened_for_credential_scout is True
        first_key = ctx.credential_scout_rescout_context_key
        assert first_key

        result = await _update_workflow({"workflow_yaml": yaml_text}, ctx)

        assert result["ok"] is False
        assert ctx.synthesized_block_reopened_for_credential_scout is False
        assert ctx.credential_scout_rescout_context_key == first_key

    @pytest.mark.asyncio
    async def test_new_binding_identity_re_arms_reopen(self) -> None:
        ctx = _code_only_ctx()
        ctx.scout_trajectory = []

        await _update_workflow({"workflow_yaml": TestCredentialScoutPersistGate._SUBMIT_CODE_YAML}, ctx)
        first_key = ctx.credential_scout_rescout_context_key
        assert ctx.synthesized_block_reopened_for_credential_scout is True

        rebound_yaml = _credential_code_yaml(
            code="""
            await page.locator("#email").fill(login_credential.username)
            await page.locator("input[type='password']").fill(login_credential.password)
            await page.locator("#totpmfa").fill(login_credential.totp)
            await page.locator("input[type='submit']").click()
            await page.wait_for_load_state("load")
            """,
            credential_id="cred_rebound",
        )
        result = await _update_workflow({"workflow_yaml": rebound_yaml}, ctx)

        assert result["ok"] is False
        assert ctx.synthesized_block_reopened_for_credential_scout is True
        assert ctx.credential_scout_rescout_context_key != first_key

    @pytest.mark.asyncio
    async def test_combined_credential_and_code_safety_reject_arms_reopen(self) -> None:
        ctx = _code_only_ctx()
        ctx.scout_trajectory = []

        result = await _update_workflow({"workflow_yaml": TestCredentialScoutPersistGate._UNSAFE_SUBMIT_CODE_YAML}, ctx)

        assert result["ok"] is False
        assert result["data"]["failure_type"] == "missing_credential_or_init"
        assert ctx.synthesized_block_reopened_for_credential_scout is True
        assert ctx.credential_scout_rescout_context_key

    @pytest.mark.asyncio
    async def test_gate_passing_attempt_leaves_window_closed(self) -> None:
        ctx = _code_only_ctx()
        ctx.scout_trajectory = []

        await _update_workflow({"workflow_yaml": TestCredentialScoutPersistGate._SUBMIT_CODE_YAML}, ctx)
        assert ctx.synthesized_block_reopened_for_credential_scout is True

        ctx.scout_trajectory = [
            _credential_fill_interaction("username"),
            _credential_fill_interaction("password"),
            _credential_fill_interaction("totp"),
            _submit_interaction(),
        ]
        result = await _update_workflow({"workflow_yaml": TestCredentialScoutPersistGate._SUBMIT_CODE_YAML}, ctx)

        assert ctx.synthesized_block_reopened_for_credential_scout is False
        error_text = str(result.get("error") or "")
        assert "fill_credential_field" not in error_text

    def test_should_impose_after_update_attempt_honors_reopen_flag(self) -> None:
        ctx = _code_only_ctx()
        assert workflow_update_module._should_impose_after_update_attempt(ctx) is False
        ctx.synthesized_block_reopened_for_credential_scout = True
        assert workflow_update_module._should_impose_after_update_attempt(ctx) is True


def _persisted_workflow_result() -> dict[str, object]:
    return {
        "ok": True,
        "_workflow": SimpleNamespace(
            workflow_definition=SimpleNamespace(blocks=[SimpleNamespace(label="quote_flow")]),
            proxy_location=None,
        ),
    }


def _quote_submitted_yaml() -> str:
    return _yaml(
        """
        title: Quote
        workflow_definition:
          blocks:
          - block_type: code
            label: quote_flow
            code: |
              await page.locator("#zip").fill(str(zip_code))
              await page.locator("#continue").click()
        """
    )


class TestGoalCompletionLandingImposition:
    def test_goal_complete_trajectory_imposes_after_mid_scout_first_authoring_call(self) -> None:
        ctx = _quote_ctx()
        ctx.update_workflow_called = True

        result = workflow_update_module._maybe_impose_synthesized_code_block(_quote_submitted_yaml(), ctx)

        assert result.violations == []
        assert result.substitutions is not None
        code = str(_single_code_block(parse_workflow_yaml(result.workflow_yaml))["code"])
        assert 'page.locator("#coverage-next")' in code
        assert "expect_download" not in code
        assert ctx.pending_goal_complete_landing is True
        assert ctx.synthesized_goal_complete_landed is False

    def test_non_goal_complete_trajectory_still_skips_imposition_after_update(self) -> None:
        ctx = _code_only_ctx()
        _enable_imposition(ctx)
        ctx.scout_trajectory = [
            {
                "tool_name": "click",
                "selector": "button",
                "source_url": "https://example.com/search",
                "trajectory_index": 0,
            }
        ]
        ctx.update_workflow_called = True
        submitted = _yaml(
            """
            title: Search
            workflow_definition:
              blocks:
              - block_type: code
                label: search
                code: |
                  await page.locator("#other").click()
            """
        )

        with capture_logs() as logs:
            result = workflow_update_module._maybe_impose_synthesized_code_block(submitted, ctx)

        assert result.workflow_yaml == submitted
        skipped = [entry for entry in logs if entry["event"] == "copilot_imposition_skipped_after_update"]
        assert len(skipped) == 1
        assert skipped[0]["goal_complete"] is False
        assert skipped[0]["synthesized_goal_complete_landed"] is False
        assert not [entry for entry in logs if entry["event"] == "copilot_imposition_admitted_after_update"]

    def test_admission_record_names_the_landing_pending_key(self) -> None:
        ctx = _quote_ctx()
        ctx.update_workflow_called = True

        with capture_logs() as logs:
            workflow_update_module._maybe_impose_synthesized_code_block(_quote_submitted_yaml(), ctx)

        admitted = [entry for entry in logs if entry["event"] == "copilot_imposition_admitted_after_update"]
        assert len(admitted) == 1
        assert admitted[0]["admission_key"] == "goal_completion_landing_pending"

    def test_landed_spine_is_not_reimposed_on_resubmission(self) -> None:
        ctx = _quote_ctx()
        ctx.update_workflow_called = True
        ctx.synthesized_goal_complete_landed = True

        with capture_logs() as logs:
            result = workflow_update_module._maybe_impose_synthesized_code_block(_quote_submitted_yaml(), ctx)

        assert result.workflow_yaml == _quote_submitted_yaml()
        assert result.substitutions is None
        skipped = [entry for entry in logs if entry["event"] == "copilot_imposition_skipped_after_update"]
        assert len(skipped) == 1
        assert skipped[0]["goal_complete"] is True
        assert skipped[0]["synthesized_goal_complete_landed"] is True

    def test_successful_update_promotes_pending_goal_completion_landing(self) -> None:
        ctx = _quote_ctx()
        ctx.update_workflow_called = True

        result = workflow_update_module._maybe_impose_synthesized_code_block(_quote_submitted_yaml(), ctx)
        ctx.persisted_draft_browser_calls = workflow_update_module._workflow_yaml_browser_call_pairs(
            result.workflow_yaml
        )
        workflow_update_module._record_workflow_update_result(ctx, _persisted_workflow_result())

        assert ctx.synthesized_goal_complete_landed is True
        assert ctx.pending_goal_complete_landing is False

    def test_premature_landing_that_under_builds_does_not_retire_and_reimposes_full_spine(self) -> None:
        ctx = _quote_ctx()
        ctx.update_workflow_called = True

        workflow_update_module._maybe_impose_synthesized_code_block(_quote_submitted_yaml(), ctx)
        assert ctx.pending_goal_complete_landing is True
        ctx.persisted_draft_browser_calls = workflow_update_module._workflow_yaml_browser_call_pairs(
            _quote_submitted_yaml()
        )
        workflow_update_module._record_workflow_update_result(ctx, _persisted_workflow_result())

        assert ctx.synthesized_goal_complete_landed is False
        assert ctx.pending_goal_complete_landing is False

        reimposed = workflow_update_module._maybe_impose_synthesized_code_block(_quote_submitted_yaml(), ctx)
        code = str(_single_code_block(parse_workflow_yaml(reimposed.workflow_yaml))["code"])
        assert 'page.locator("#coverage-next")' in code
        assert ctx.pending_goal_complete_landing is True

    def test_armed_landing_reads_current_spine_not_arm_snapshot_and_does_not_retire(self) -> None:
        ctx = _quote_ctx()
        ctx.pending_goal_complete_landing = True
        ctx.persisted_draft_browser_calls = workflow_update_module._workflow_yaml_browser_call_pairs(
            _quote_submitted_yaml()
        )

        workflow_update_module._record_workflow_update_result(ctx, _persisted_workflow_result())

        assert ctx.synthesized_goal_complete_landed is False
        assert ctx.pending_goal_complete_landing is False

        reimposed = workflow_update_module._maybe_impose_synthesized_code_block(_quote_submitted_yaml(), ctx)
        code = str(_single_code_block(parse_workflow_yaml(reimposed.workflow_yaml))["code"])
        assert 'page.locator("#coverage-next")' in code

    def test_full_spine_plus_suffix_draft_stays_retired_without_reimposition(self) -> None:
        ctx = _quote_ctx()
        ctx.update_workflow_called = True
        ctx.synthesized_goal_complete_landed = True
        submitted = _yaml(
            """
            title: Quote
            workflow_definition:
              blocks:
              - block_type: code
                label: quote_flow
                code: |
                  await page.locator("#zip").fill(str(zip_code))
                  await page.locator("#continue").click()
                  await page.locator("#coverage-next").click()
                  records = [{"quote": "captured"}]
            """
        )

        with capture_logs() as logs:
            result = workflow_update_module._maybe_impose_synthesized_code_block(submitted, ctx)

        assert result.workflow_yaml == submitted
        assert result.substitutions is None
        assert result.violations == []
        skipped = [entry for entry in logs if entry["event"] == "copilot_imposition_skipped_after_update"]
        assert len(skipped) == 1
        assert skipped[0]["synthesized_goal_complete_landed"] is True
        assert ctx.synthesized_goal_complete_landed is True
        assert ctx.pending_goal_complete_landing is False

    def test_successful_update_promotes_pending_extraction_candidate(self) -> None:
        ctx = _live_read_extraction_ctx()

        result = workflow_update_module._maybe_impose_synthesized_code_block(_live_read_submitted_yaml(), ctx)

        assert result.violations == []
        assert ctx.requested_output_extraction_candidate is None
        pending_candidate = ctx.pending_requested_output_extraction_candidate
        assert pending_candidate is not None

        workflow_update_module._record_workflow_update_result(ctx, _persisted_workflow_result())

        assert ctx.requested_output_extraction_candidate == pending_candidate
        assert ctx.pending_requested_output_extraction_candidate is None

    def test_failed_update_leaves_committed_candidate_and_landing_latch_untouched(self) -> None:
        ctx = _live_read_extraction_ctx()

        workflow_update_module._maybe_impose_synthesized_code_block(_live_read_submitted_yaml(), ctx)
        workflow_update_module._record_workflow_update_result(ctx, {"ok": False})

        assert ctx.requested_output_extraction_candidate is None
        assert ctx.synthesized_goal_complete_landed is False

    def test_rejected_imposition_does_not_mutate_committed_candidate(self) -> None:
        ctx = _live_read_extraction_ctx()
        workflow_update_module._maybe_impose_synthesized_code_block(_live_read_submitted_yaml(), ctx)
        workflow_update_module._record_workflow_update_result(ctx, _persisted_workflow_result())
        committed = ctx.requested_output_extraction_candidate
        assert committed is not None
        ctx.synthesized_block_reopened_after_failed_run = True
        # A retained plan reaches the goal and admits via the landing lane instead of rejecting;
        # this contract is about the rejecting reopen path, so pin reach off.
        ctx.last_bound_requested_output_extraction_plan = None

        unscouted_sibling = _yaml(
            f"""
            title: Record lookup
            workflow_definition:
              blocks:
              - block_type: code
                label: {workflow_update_module._SYNTHESIZED_BLOCK_LABEL}
                code: |
                  await page.locator("#search-submit").click()
              - block_type: code
                label: helper_stage
                code: |
                  await page.locator("#surprise").click()
            """
        )
        result = workflow_update_module._maybe_impose_synthesized_code_block(unscouted_sibling, ctx)

        assert result.violations
        assert ctx.requested_output_extraction_candidate == committed
        assert ctx.pending_requested_output_extraction_candidate is None

    def test_read_reach_admits_reimposition_and_keeps_the_committed_candidate(self) -> None:
        # The retained plan reaches the goal for a read deliverable (SKY-13485), so the same reopen
        # state admits via goal_reaching_spine_unlanded and re-lands the spine instead of rejecting.
        ctx = _live_read_extraction_ctx()
        workflow_update_module._maybe_impose_synthesized_code_block(_live_read_submitted_yaml(), ctx)
        workflow_update_module._record_workflow_update_result(ctx, _persisted_workflow_result())
        committed = ctx.requested_output_extraction_candidate
        assert committed is not None
        assert ctx.last_bound_requested_output_extraction_plan is not None
        ctx.synthesized_block_reopened_after_failed_run = True

        result = workflow_update_module._maybe_impose_synthesized_code_block(_live_read_submitted_yaml(), ctx)

        assert not result.violations
        assert workflow_update_module._imposition_admission_key_after_update(ctx) == "goal_reaching_spine_unlanded"
        pending = ctx.pending_requested_output_extraction_candidate
        assert pending is not None and pending.fingerprint == committed.fingerprint


def _hand_authored_rung_yaml() -> str:
    return _yaml(
        """
        title: Quote
        workflow_definition:
          blocks:
          - block_type: code
            label: enter_zip
            code: |
              await page.locator("#zip").fill(str(zip_code))
          - block_type: code
            label: continue_step
            code: |
              await page.locator("#continue").click()
          - block_type: code
            label: coverage_step
            code: |
              await page.locator("#coverage-next").click()
        """
    )


class TestAdmittedImpositionOwnsSpineCoverage:
    def test_multi_block_hand_authored_draft_lands_spine_on_carrier_without_stale_rungs(self) -> None:
        ctx = _quote_ctx()
        ctx.update_workflow_called = True

        result = workflow_update_module._maybe_impose_synthesized_code_block(_hand_authored_rung_yaml(), ctx)

        assert result.violations == []
        assert result.substitutions is not None
        parsed = parse_workflow_yaml(result.workflow_yaml)
        blocks = _code_blocks(parsed)
        assert list(blocks) == ["enter_zip"]
        code = str(blocks["enter_zip"]["code"])
        assert 'page.locator("#continue")' in code
        assert code.count('page.locator("#coverage-next")') == 1
        assert ctx.spine_imposition_owned_attempt is True

    def test_referenced_stale_rung_label_steers_instead_of_being_dropped(self) -> None:
        ctx = _quote_ctx()
        ctx.update_workflow_called = True
        submitted = _yaml(
            """
            title: Quote
            workflow_definition:
              blocks:
              - block_type: code
                label: enter_zip
                code: |
                  await page.locator("#zip").fill(str(zip_code))
              - block_type: code
                label: coverage_step
                code: |
                  await page.locator("#coverage-next").click()
              - block_type: code
                label: summarize
                code: |
                  return {"output": coverage_step_output}
            """
        )

        result = workflow_update_module._maybe_impose_synthesized_code_block(submitted, ctx)

        assert result.substitutions is None
        assert any("`coverage_step`" in violation for violation in result.violations)
        assert result.workflow_yaml == submitted

    def test_graph_referenced_stale_rung_steers_instead_of_being_dropped(self) -> None:
        # A surviving block's next_block_label points at the stale rung; dropping it would manufacture the
        # dangling reference the AC4 gate rejects, so it must refuse-with-provenance, not silently delete.
        stale = {
            "block_type": "code",
            "label": "extract_priority_resale_document",
            "code": "await page.locator('#doc').click()",
        }
        parsed: dict = {
            "workflow_definition": {
                "blocks": [
                    {
                        "block_type": "code",
                        "label": "order_lookup",
                        "next_block_label": "extract_priority_resale_document",
                        "code": "await page.goto('https://example.com')",
                    },
                    stale,
                ]
            }
        }

        result = workflow_update_module._drop_stale_spine_rung_blocks(
            parsed, [stale], carrier_label="carrier", provenance=[]
        )

        assert result.violation is not None
        assert "`extract_priority_resale_document`" in result.violation
        assert result.replaced_labels == []
        remaining = [block.get("label") for block in parsed["workflow_definition"]["blocks"]]
        assert "extract_priority_resale_document" in remaining

    def test_unreferenced_stale_rung_is_still_dropped(self) -> None:
        stale = {
            "block_type": "code",
            "label": "orphan_rung",
            "code": "await page.locator('#doc').click()",
        }
        parsed: dict = {
            "workflow_definition": {
                "blocks": [
                    {"block_type": "code", "label": "carrier", "code": "await page.goto('https://example.com')"},
                    stale,
                ]
            }
        }

        result = workflow_update_module._drop_stale_spine_rung_blocks(
            parsed, [stale], carrier_label="carrier", provenance=[]
        )

        assert result.violation is None
        assert result.replaced_labels == ["orphan_rung"]
        remaining = [block.get("label") for block in parsed["workflow_definition"]["blocks"]]
        assert "orphan_rung" not in remaining

    def test_pre_persist_gate_still_fires_when_imposition_is_not_admitted(self) -> None:
        ctx = _records_spine_ctx()
        ctx.update_workflow_called = True
        under_built = _records_block_yaml('await page.locator("#stage-a").click()')

        imposition = workflow_update_module._maybe_impose_synthesized_code_block(under_built, ctx)

        assert ctx.spine_imposition_owned_attempt is False
        assert imposition.substitutions is None

        with capture_logs() as logs:
            guarded = workflow_update_module._pre_persist_scouted_spine_result(imposition.workflow_yaml, ctx)

        assert guarded is not None
        assert any("scouted_spine_under_build" in violation for violation in guarded.violations)
        events = [log for log in logs if log["event"] == "copilot_scouted_spine_under_build"]
        assert events and events[0]["site"] == "pre_persist"

    def test_pre_persist_observation_is_gated_on_the_repeated_omission_latch(self) -> None:
        ctx = _records_spine_ctx()
        ctx.update_workflow_called = True
        under_built = _records_block_yaml('await page.locator("#stage-a").click()')

        with capture_logs() as unlatched_logs:
            assert workflow_update_module._current_draft_repeats_prior_scouted_spine_omission(under_built, ctx) is False
        assert not [log for log in unlatched_logs if log["event"] == "copilot_scouted_spine_under_build"]

        probe = workflow_update_module._pre_persist_scouted_spine_result(under_built, ctx)
        assert probe is not None
        ctx.scouted_spine_repeated_identical_missing_steps = True
        ctx.scouted_spine_previous_omission_digest = probe.omission_digest

        with capture_logs() as latched_logs:
            assert workflow_update_module._current_draft_repeats_prior_scouted_spine_omission(under_built, ctx) is True
        events = [log for log in latched_logs if log["event"] == "copilot_scouted_spine_under_build"]
        assert [event["site"] for event in events] == ["pre_persist"]

    def test_ungrounded_sibling_mixing_ambiguous_and_concrete_calls_is_replaced_by_the_spine(self) -> None:
        ctx = _quote_ctx()
        ctx.update_workflow_called = True
        submitted = _yaml(
            """
            title: Quote
            workflow_definition:
              blocks:
              - block_type: code
                label: quote_flow
                code: |
                  await page.locator("#zip").fill(str(zip_code))
                  await page.locator("#continue").click()
              - block_type: code
                label: download_matching_invoice
                code: |
                  async with page.expect_download(timeout=20000) as download_info:
                      await page.get_by_role("link", name="View Printable Statement").click()
                  download = await download_info.value
              - block_type: code
                label: summarize
                code: |
                  records = [{"invoice": "parsed"}]
            """
        )

        with capture_logs() as logs:
            result = workflow_update_module._maybe_impose_synthesized_code_block(submitted, ctx)

        assert result.violations == []
        assert ctx.spine_imposition_owned_attempt is True
        blocks = _code_blocks(parse_workflow_yaml(result.workflow_yaml))
        assert list(blocks) == ["quote_flow", "summarize"]
        carrier_code = str(blocks["quote_flow"]["code"])
        assert "get_by_role" not in carrier_code
        assert carrier_code.count('page.locator("#coverage-next").click()') == 1
        dropped = [log for log in logs if log["event"] == "copilot_spine_stale_rung_dropped"]
        assert dropped and dropped[0]["dropped_labels"] == ["download_matching_invoice"]
        assert "never_captured" in {record["kind"] for record in dropped[0]["dropped_provenance"]}
        assert result.substitutions is not None
        assert result.substitutions["replaced_hand_authored_browser_rungs"] == ["download_matching_invoice"]

    def test_referenced_ungrounded_sibling_steers_instead_of_being_replaced(self) -> None:
        ctx = _quote_ctx()
        ctx.update_workflow_called = True
        submitted = _yaml(
            """
            title: Quote
            workflow_definition:
              blocks:
              - block_type: code
                label: quote_flow
                code: |
                  await page.locator("#zip").fill(str(zip_code))
                  await page.locator("#continue").click()
              - block_type: code
                label: download_matching_invoice
                code: |
                  async with page.expect_download(timeout=20000) as download_info:
                      await page.get_by_role("link", name="View Printable Statement").click()
                  download = await download_info.value
              - block_type: code
                label: summarize
                code: |
                  return {"output": download_matching_invoice_output}
            """
        )

        result = workflow_update_module._maybe_impose_synthesized_code_block(submitted, ctx)

        assert result.substitutions is None
        assert result.workflow_yaml == submitted
        assert any("`download_matching_invoice`" in violation for violation in result.violations)
        assert any("never_captured" in violation for violation in result.violations)
        assert any("never by authoring browser calls freehand" in violation for violation in result.violations)


def _reaching_extraction_ctx() -> CopilotContext:
    ctx = _live_read_extraction_ctx()
    ctx.reached_download_target = ReachedDownloadTarget(
        selector='a[href="/files/report.pdf"]',
        affordance_text="Download PDF",
        download_kind="extension",
        source_step="trajectory_recency",
        already_registered=False,
    )
    return ctx


def _commit_only_ctx() -> CopilotContext:
    ctx = _code_only_ctx()
    _enable_imposition(ctx)
    criteria = tuple(
        CompletionCriterion(
            id=field,
            outcome=field.replace("_", " "),
            output_path=f"output.{field}",
        )
        for field in (
            "confirmation_number",
            "account_number",
            "selected_start_date",
            "deposit_amount",
            "next_owner",
        )
    )
    ctx.request_policy = RequestPolicy(completion_criteria=list(criteria))
    ctx.completion_criteria_turn_state = SimpleNamespace(decision=SimpleNamespace(criteria=criteria))
    ctx.flow_evidence = []
    ctx.scout_trajectory = [
        {
            "tool_name": "click",
            "selector": 'button[data-action="openForm"]',
            "source_url": "https://example.test/start-service",
            "trajectory_index": 0,
        },
        {
            "tool_name": "click",
            "selector": 'button[data-action="commitForm"]',
            "source_url": "https://example.test/start-service",
            "trajectory_index": 1,
        },
    ]
    return ctx


def _commit_only_submitted_yaml() -> str:
    return _yaml(
        """
        title: Start service
        workflow_definition:
          blocks:
          - block_type: code
            label: start_service
            code: |
              await page.locator('button[data-action="openForm"]').click()
        """
    )


class TestBoundExtractionPlanIsAnsweredWith:
    def test_a_plan_that_bound_survives_evidence_that_no_longer_derives_one(self) -> None:
        # A live turn derived once and degraded on the next sixteen evaluations, so the bound plan was
        # gone by the imposition that needed it and the generated read invented its own locator.
        ctx = _live_read_extraction_ctx()

        bound = enforcement_module.requested_output_extraction_plan(ctx)

        assert bound is not None
        assert ctx.last_bound_requested_output_extraction_plan is bound

        ctx.flow_evidence = []

        assert enforcement_module.requested_output_extraction_plan(ctx) is bound

    def test_a_retained_plan_is_abandoned_once_the_request_asks_for_other_paths(self) -> None:
        ctx = _live_read_extraction_ctx()
        assert enforcement_module.requested_output_extraction_plan(ctx) is not None

        ctx.flow_evidence = []
        ctx.request_policy = RequestPolicy(
            completion_criteria=[
                CompletionCriterion(id="other", outcome="Other Field", output_path="output.other_field")
            ]
        )
        ctx.completion_criteria_turn_state = SimpleNamespace(
            decision=SimpleNamespace(criteria=tuple(ctx.request_policy.completion_criteria))
        )

        assert enforcement_module.requested_output_extraction_plan(ctx) is None


class TestCommitOnlyReach:
    def test_commit_only_two_click_trajectory_reaches_without_output_binding(self) -> None:
        ctx = _commit_only_ctx()

        assert enforcement_module.requested_output_extraction_plan(ctx) is None
        assert enforcement_module.synthesized_trajectory_reaches_goal(ctx) is True
        assert enforcement_module.synthesized_trajectory_is_goal_complete(ctx) is False

    @pytest.mark.parametrize(
        "trajectory",
        [
            [
                {
                    "tool_name": "click",
                    "selector": 'button[data-action="commitForm"]',
                    "source_url": "https://example.test/start-service",
                    "trajectory_index": 0,
                }
            ],
            [
                {
                    "tool_name": "click",
                    "selector": 'button[data-action="openForm"]',
                    "source_url": "https://example.test/start-service",
                    "trajectory_index": 0,
                },
                {
                    "tool_name": "click",
                    "selector": "button",
                    "role": "button",
                    "source_url": "https://example.test/start-service",
                    "trajectory_index": 1,
                },
            ],
            [
                {
                    "tool_name": "click",
                    "selector": 'button[data-action="openForm"]',
                    "source_url": "https://example.test/start-service",
                },
                {
                    "tool_name": "click",
                    "selector": 'button[data-action="commitForm"]',
                    "source_url": "https://example.test/start-service",
                },
            ],
            [
                {
                    "tool_name": "click",
                    "selector": 'button[data-action="openForm"]',
                    "source_url": "https://example.test/start-service",
                    "trajectory_index": 2,
                },
                {
                    "tool_name": "click",
                    "selector": 'button[data-action="commitForm"]',
                    "source_url": "https://example.test/start-service",
                    "trajectory_index": 1,
                },
            ],
            [
                {
                    "tool_name": "navigate",
                    "url": "https://example.test/start-service",
                    "source_url": "https://example.test",
                    "trajectory_index": 0,
                }
            ],
            [
                {
                    "tool_name": "click",
                    "selector": 'button[data-action="loginForm"]',
                    "source_url": "https://example.test/login",
                    "trajectory_index": 0,
                },
                {
                    "tool_name": "fill_credential_field",
                    "selector": "#username",
                    "source_url": "https://example.test/login",
                    "credential_id": "cred_1",
                    "credential_field": "username",
                    "trajectory_index": 1,
                },
                {
                    "tool_name": "click",
                    "selector": "button",
                    "role": "button",
                    "source_url": "https://example.test/login",
                    "trajectory_index": 2,
                },
            ],
        ],
    )
    def test_commit_only_exclusions_do_not_reach(self, trajectory: list[dict[str, object]]) -> None:
        ctx = _commit_only_ctx()
        ctx.scout_trajectory = trajectory

        assert enforcement_module.synthesized_trajectory_reaches_goal(ctx) is False

    def test_commit_only_imposition_uses_goal_reaching_admission(self) -> None:
        ctx = _commit_only_ctx()
        ctx.update_workflow_called = True

        with capture_logs() as logs:
            result = workflow_update_module._maybe_impose_synthesized_code_block(_commit_only_submitted_yaml(), ctx)

        assert result.violations == []
        assert result.substitutions
        assert ctx.spine_imposition_owned_attempt is True
        code = str(_single_code_block(parse_workflow_yaml(result.workflow_yaml))["code"])
        assert "openForm" in code
        assert "commitForm" in code
        admitted = [entry for entry in logs if entry["event"] == "copilot_imposition_admitted_after_update"]
        assert len(admitted) == 1
        assert admitted[0]["admission_key"] == "goal_reaching_spine_unlanded"
        assert admitted[0]["admission_key"] != "reopen_author_time_reject"


class TestReachingTrajectoryOwnershipDeterminism:
    def test_owned_attempt_fires_before_the_extraction_plan_materializes(self) -> None:
        ctx = _reaching_extraction_ctx()
        ctx.flow_evidence = []
        ctx.update_workflow_called = True

        with capture_logs() as logs:
            result = workflow_update_module._maybe_impose_synthesized_code_block(_live_read_submitted_yaml(), ctx)

        assert enforcement_module.synthesized_trajectory_reaches_goal(ctx) is True
        assert enforcement_module.synthesized_trajectory_is_goal_complete(ctx) is False
        assert result.violations == []
        assert ctx.spine_imposition_owned_attempt is True
        assert len([log for log in logs if log["event"] == "copilot_spine_imposition_owned_attempt"]) == 1
        code = str(_single_code_block(parse_workflow_yaml(result.workflow_yaml))["code"])
        assert "_extraction_value_0" not in code
        assert ctx.pending_requested_output_extraction_candidate is None

    def test_materialized_plan_reimposes_idempotently_and_holds_candidate_identity(self) -> None:
        ctx = _reaching_extraction_ctx()
        ctx.flow_evidence = []
        ctx.update_workflow_called = True
        first = workflow_update_module._maybe_impose_synthesized_code_block(_live_read_submitted_yaml(), ctx)
        assert first.violations == []

        ctx.flow_evidence = _live_read_extraction_ctx().flow_evidence
        second = workflow_update_module._maybe_impose_synthesized_code_block(first.workflow_yaml, ctx)

        assert second.violations == []
        assert ctx.spine_imposition_owned_attempt is True
        code = str(_single_code_block(parse_workflow_yaml(second.workflow_yaml))["code"])
        assert code.count('return {"output": {"record_id": _extraction_value_0}}') == 1
        candidate = ctx.pending_requested_output_extraction_candidate
        assert candidate is not None

        third = workflow_update_module._maybe_impose_synthesized_code_block(second.workflow_yaml, ctx)

        assert third.violations == []
        assert ctx.pending_requested_output_extraction_candidate == candidate
        assert str(_single_code_block(parse_workflow_yaml(third.workflow_yaml))["code"]) == code

    def test_reaching_attempt_without_a_carrier_records_the_absence(self) -> None:
        ctx = _quote_ctx()
        ctx.update_workflow_called = True
        blockless = _yaml(
            """
            title: Quote
            workflow_definition:
              blocks: []
            """
        )

        with capture_logs() as logs:
            result = workflow_update_module._maybe_impose_synthesized_code_block(blockless, ctx)

        assert result.violations == []
        assert result.substitutions is None
        assert ctx.spine_imposition_owned_attempt is False
        assert [log for log in logs if log["event"] == "copilot_spine_imposition_no_carrier"]


def _download_ctx() -> CopilotContext:
    ctx = _quote_ctx()
    ctx.scout_trajectory = list(ctx.scout_trajectory) + [
        {
            "tool_name": "click",
            "selector": "#view-printable",
            "source_url": "https://example.com/quote/statement",
            "trajectory_index": 3,
        }
    ]
    ctx.reached_download_target = ReachedDownloadTarget(
        selector='a[href="/files/report.pdf"]',
        affordance_text="Download PDF",
        download_kind="extension",
        source_step="trajectory_recency",
        already_registered=False,
        trajectory_anchor=2,
    )
    return ctx


def _download_ctx_with_criterion(evidence_source: RequestedOutputEvidenceSource) -> CopilotContext:
    ctx = _download_ctx()
    ctx.turn_id = "t-download"
    ctx.update_workflow_called = True
    ctx.request_policy = RequestPolicy(
        completion_criteria=[
            CompletionCriterion(
                id="downloaded_statement",
                outcome="the statement file is downloaded",
                output_path="output.downloads",
                requested_output_evidence_source=evidence_source,
            )
        ]
    )
    return ctx


class TestSequencedDownloadTerminalCoverage:
    def test_imposed_spine_stops_at_the_capture_anchor_and_ends_on_the_terminal(self) -> None:
        ctx = _download_ctx()
        ctx.update_workflow_called = True

        result = workflow_update_module._maybe_impose_synthesized_code_block(_quote_submitted_yaml(), ctx)

        assert result.violations == []
        assert result.substitutions is not None
        code = str(_single_code_block(parse_workflow_yaml(result.workflow_yaml))["code"])
        assert '"#view-printable"' not in code
        assert code.index("async with page.expect_download()") > code.index('"#coverage-next"')

    def test_landed_sequenced_spine_satisfies_the_under_build_guard(self) -> None:
        ctx = _download_ctx()
        ctx.update_workflow_called = True

        imposed = workflow_update_module._maybe_impose_synthesized_code_block(_quote_submitted_yaml(), ctx)

        later = _download_ctx()
        later.update_workflow_called = True

        with capture_logs() as logs:
            guarded = workflow_update_module._pre_persist_scouted_spine_result(imposed.workflow_yaml, later)

        assert guarded is None
        assert not [log for log in logs if log["event"] == "copilot_scouted_spine_under_build"]

    def test_sequenced_spine_with_runtime_output_criterion_stays_fail_closed(self) -> None:
        ctx = _download_ctx_with_criterion("runtime_output")

        imposed = workflow_update_module._maybe_impose_synthesized_code_block(_quote_submitted_yaml(), ctx)

        assert imposed.violations == []
        assert workflow_update_module._output_contract_required_paths_source(ctx).union == {"output.downloads"}

    def test_retired_imposition_still_coverage_checks_a_later_under_built_persist(self) -> None:
        ctx = _quote_ctx()
        ctx.update_workflow_called = True
        ctx.synthesized_goal_complete_landed = True

        imposition = workflow_update_module._maybe_impose_synthesized_code_block(_quote_submitted_yaml(), ctx)

        assert imposition.substitutions is None
        assert ctx.spine_imposition_owned_attempt is False

        guarded = workflow_update_module._pre_persist_scouted_spine_result(imposition.workflow_yaml, ctx)

        assert guarded is not None
        assert any("scouted_spine_under_build" in violation for violation in guarded.violations)


def _scalar_binding(output_path: str, label: str, selector: str) -> LiveReadBinding:
    return LiveReadBinding(
        output_path=output_path,
        kind=LiveReadKind.KEY_VALUE,
        selector=selector,
        selector_count=1,
        selector_index=0,
        child_index=1,
        child_count=2,
        relation_label=label,
    )


def _scalar_plan(*bindings: LiveReadBinding) -> RequestedOutputExtractionPlan:
    return RequestedOutputExtractionPlan(
        requested_output_paths=tuple(binding.output_path for binding in bindings),
        observation_step=1,
        observation_identity="obs-identity",
        reveal=RevealAnchor(selector="#reveal"),
        live_reads=tuple(bindings),
        identity="plan-identity",
    )


class TestReadDeliverableReach:
    """A read deliverable has no commit shape to reach; its bound extraction plan is the reach
    evidence (SKY-13485). A login+read scout trajectory satisfies no commit clause, so without
    this the imposition lane never opens and the compiled read never lands."""

    _READ_PATH = "output.azure_error_count"

    def _read_ctx(
        self,
        *criteria: CompletionCriterion,
        plan: RequestedOutputExtractionPlan | None,
    ) -> CopilotContext:
        ctx = _code_only_ctx()
        ctx.scout_trajectory = [
            {
                "tool_name": "read_value",
                "read_expression": "document.querySelector('.tile').innerText",
                "read_output_path": self._READ_PATH,
                "trajectory_index": 0,
                "source_url": "https://dashboard.example.test/records",
            }
        ]
        ctx.completion_criteria_turn_state = SimpleNamespace(decision=SimpleNamespace(criteria=tuple(criteria)))
        ctx.last_bound_requested_output_extraction_plan = plan
        return ctx

    def _read_criterion(self) -> CompletionCriterion:
        return CompletionCriterion(
            id="azure_error_count",
            outcome="the number of azure errors is returned",
            output_path=self._READ_PATH,
        )

    def _bound_plan(self) -> RequestedOutputExtractionPlan:
        return _scalar_plan(_scalar_binding(self._READ_PATH, "records found", ".tile"))

    def test_read_trajectory_with_bound_plan_reaches_goal(self) -> None:
        ctx = self._read_ctx(self._read_criterion(), plan=self._bound_plan())

        assert enforcement_module.synthesized_trajectory_reaches_goal(ctx) is True

    def test_read_trajectory_without_plan_does_not_reach(self) -> None:
        ctx = self._read_ctx(self._read_criterion(), plan=None)

        assert enforcement_module.synthesized_trajectory_reaches_goal(ctx) is False

    def test_plan_bound_to_other_paths_does_not_reach(self) -> None:
        other = _scalar_plan(_scalar_binding("output.visitor_count", "Visitors", ".other"))
        ctx = self._read_ctx(self._read_criterion(), plan=other)

        assert enforcement_module.synthesized_trajectory_reaches_goal(ctx) is False

    def test_unreached_download_request_ignores_the_plan_latch(self) -> None:
        download = CompletionCriterion(
            id="invoice_pdf",
            outcome="the invoice PDF is downloaded",
            output_path="output.invoice_pdf",
            deliverable_kind="registered_download",
        )
        ctx = self._read_ctx(self._read_criterion(), download, plan=self._bound_plan())
        ctx.reached_download_target = None

        assert enforcement_module.synthesized_trajectory_reaches_goal(ctx) is False

    def test_a_mandated_action_still_outstanding_is_not_reached(self) -> None:
        # A method-mandated action never routes to the commit shape, so the read clause is the only
        # thing standing between a bound plan and a "goal reached" on a spine that never acted.
        mandated = CompletionCriterion(
            id="durable_fill",
            outcome="the live form is filled on the page this turn",
            kind="terminal_action",
            terminal_action_family="form",
            method_mandated=True,
        )
        ctx = self._read_ctx(self._read_criterion(), mandated, plan=self._bound_plan())

        assert enforcement_module.synthesized_trajectory_reaches_goal(ctx) is False

    def test_terminal_action_request_keeps_the_commit_shape(self) -> None:
        terminal = CompletionCriterion(
            id="start_service_request",
            outcome="the start-service request reaches its review page",
            kind="terminal_action",
            terminal_action_family="request",
        )
        ctx = self._read_ctx(self._read_criterion(), terminal, plan=self._bound_plan())

        assert enforcement_module.synthesized_trajectory_reaches_goal(ctx) is False


_MIXED_DOWNLOAD_CODE = (
    "async with page.expect_download() as dl_info:\n"
    '    await page.locator("#download-invoice").click()\n'
    "dl_info_file = await dl_info.value\n"
    "downloaded_file_name = dl_info_file.suggested_filename\n"
    "await dl_info_file.path()\n"
    "return {\n"
    '    "downloaded_file_name": downloaded_file_name,\n'
    "}\n"
)


def test_producer_scalar_coverage_single_return_over_all_paths() -> None:
    plan = _scalar_plan(
        _scalar_binding("output.account_number", "Account Number", ".acct"),
        _scalar_binding("output.confirmation_number", "Confirmation Number", ".conf"),
    )
    envelope = produce_covered_static_return_envelope(
        'await page.locator("#lookup").click()\n',
        plan=plan,
        scalar_required_paths={"output.account_number", "output.confirmation_number"},
        declaration_paths=set(),
        download_required_paths=set(),
        expects_download=False,
    )
    assert isinstance(envelope, ProducedStaticReturnEnvelope)
    assert envelope.keyed_paths == ("output.account_number", "output.confirmation_number")
    assert envelope.code.count("return {") == 1
    produced = workflow_update_module._code_block_produced_output_paths(envelope.code)
    assert {"output.account_number", "output.confirmation_number"} <= produced
    _, violations = workflow_update_module._extraction_code_with_required_static_return(
        envelope.code, required_paths={"output.account_number", "output.confirmation_number"}
    )
    assert violations == []


def test_producer_mixed_merged_single_return_passes_download_gate() -> None:
    plan = _scalar_plan(
        _scalar_binding("output.statement_amount", "Amount Due", ".amt"),
        _scalar_binding("output.statement_date", "Statement Date", ".dt"),
    )
    envelope = produce_covered_static_return_envelope(
        _MIXED_DOWNLOAD_CODE,
        plan=plan,
        scalar_required_paths={"output.statement_amount", "output.statement_date"},
        declaration_paths=set(),
        download_required_paths={"output.downloaded_invoice_pdf"},
        expects_download=True,
    )
    assert isinstance(envelope, ProducedStaticReturnEnvelope)
    assert envelope.code.count("return {") == 1
    assert '"downloaded_file_name": downloaded_file_name' in envelope.code
    assert workflow_update_module._code_uses_expect_download(envelope.code)
    _, violations = workflow_update_module._extraction_code_with_required_static_return(
        envelope.code, required_paths={"output.statement_amount", "output.statement_date"}
    )
    assert violations == []


def test_producer_no_coverage_abstains() -> None:
    plan = _scalar_plan(_scalar_binding("output.account_number", "Account Number", ".acct"))
    envelope = produce_covered_static_return_envelope(
        'await page.locator("#lookup").click()\n',
        plan=plan,
        scalar_required_paths={"output.account_number", "output.confirmation_number"},
        declaration_paths=set(),
        download_required_paths=set(),
        expects_download=False,
    )
    assert envelope is None


def test_producer_output_passes_keyer_on_reduced_set() -> None:
    plan = _scalar_plan(_scalar_binding("output.amount", "Amount", ".amt"))
    envelope = produce_covered_static_return_envelope(
        'await page.locator("#lookup").click()\n',
        plan=plan,
        scalar_required_paths={"output.amount"},
        declaration_paths={"output.optional_note"},
        download_required_paths=set(),
        expects_download=False,
    )
    assert envelope is not None
    _, violations = workflow_update_module._extraction_code_with_required_static_return(
        envelope.code, required_paths={"output.amount"}, declaration_paths={"output.optional_note"}
    )
    assert violations == []


def test_producer_abstains_when_unbindable_judgment_path_kept_in_scalar_set() -> None:
    plan = _scalar_plan(_scalar_binding("output.record_id", "Record Identifier", ".record-kv"))
    envelope = produce_covered_static_return_envelope(
        'await page.locator("#lookup").click()\n',
        plan=plan,
        scalar_required_paths={"output.record_id", "output.login_gate_blocks_target"},
        declaration_paths=set(),
        download_required_paths=set(),
        expects_download=False,
    )
    assert envelope is None


def test_producer_emits_keyed_scalar_when_judgment_path_excluded() -> None:
    plan = _scalar_plan(_scalar_binding("output.record_id", "Record Identifier", ".record-kv"))
    envelope = produce_covered_static_return_envelope(
        'await page.locator("#lookup").click()\n',
        plan=plan,
        scalar_required_paths={"output.record_id"},
        declaration_paths=set(),
        download_required_paths=set(),
        expects_download=False,
    )
    assert isinstance(envelope, ProducedStaticReturnEnvelope)
    assert envelope.keyed_paths == ("output.record_id",)
    assert "login_gate_blocks_target" not in envelope.code
    produced = workflow_update_module._code_block_produced_output_paths(envelope.code)
    assert "output.record_id" in produced
    assert "output.login_gate_blocks_target" not in produced


def test_producer_branchy_return_abstains() -> None:
    plan = _scalar_plan(_scalar_binding("output.amount", "Amount", ".amt"))
    envelope = produce_covered_static_return_envelope(
        'if await page.locator("#done").is_visible():\n    return {"output": {}}\n',
        plan=plan,
        scalar_required_paths={"output.amount"},
        declaration_paths=set(),
        download_required_paths=set(),
        expects_download=False,
    )
    assert envelope is None


def test_producer_download_without_idiom_abstains() -> None:
    plan = _scalar_plan(_scalar_binding("output.statement_amount", "Amount Due", ".amt"))
    envelope = produce_covered_static_return_envelope(
        _MIXED_DOWNLOAD_CODE,
        plan=plan,
        scalar_required_paths={"output.statement_amount"},
        declaration_paths=set(),
        download_required_paths={"output.downloaded_invoice_pdf"},
        expects_download=False,
    )
    assert envelope is None


def test_producer_identifier_collision_abstains() -> None:
    plan = _scalar_plan(_scalar_binding("output.amount", "Amount", ".amt"))
    envelope = produce_covered_static_return_envelope(
        '_envelope_value_0 = 1\nawait page.locator("#lookup").click()\n',
        plan=plan,
        scalar_required_paths={"output.amount"},
        declaration_paths=set(),
        download_required_paths=set(),
        expects_download=False,
    )
    assert envelope is None


def test_producer_declaration_paths_are_none_defaults() -> None:
    plan = _scalar_plan(_scalar_binding("output.amount", "Amount", ".amt"))
    envelope = produce_covered_static_return_envelope(
        'await page.locator("#lookup").click()\n',
        plan=plan,
        scalar_required_paths={"output.amount"},
        declaration_paths={"output.optional_note"},
        download_required_paths=set(),
        expects_download=False,
    )
    assert envelope is not None
    assert '"optional_note": None' in envelope.code
    assert "optional_note" not in envelope.code.split('"optional_note": None')[0]


def test_producer_generated_code_raises_on_empty_scalar_text() -> None:
    plan = _scalar_plan(_scalar_binding("output.amount", "Amount", ".amt"))
    envelope = produce_covered_static_return_envelope(
        'await page.locator("#lookup").click()\n',
        plan=plan,
        scalar_required_paths={"output.amount"},
        declaration_paths=set(),
        download_required_paths=set(),
        expects_download=False,
    )
    assert envelope is not None
    tree = ast.parse(envelope.code)
    empty_guards = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.UnaryOp)
        and isinstance(node.test.op, ast.Not)
        and any(isinstance(child, ast.Raise) for child in node.body)
    ]
    assert empty_guards


def _table_binding(output_path: str, column_index: int) -> LiveReadBinding:
    return LiveReadBinding(
        output_path=output_path,
        kind=LiveReadKind.TABLE_COLUMN,
        selector="#records",
        selector_count=1,
        selector_index=0,
        row_selector="#records > tbody > tr",
        row_count=2,
        column_index=column_index,
        relation_label=output_path.rsplit(".", 1)[-1],
        headers=("Location", "Status"),
        row_cell_counts=(2, 2),
        row_identities=("Alpha Open", "Beta Closed"),
    )


def test_producer_table_column_emits_credited_list_literal_and_revalidates() -> None:
    plan = _scalar_plan(
        _table_binding("output.locations[].location", 0),
        _table_binding("output.locations[].status", 1),
    )
    required = {"output.locations[].location", "output.locations[].status"}
    envelope = produce_covered_static_return_envelope(
        'await page.locator("#lookup").click()\n',
        plan=plan,
        scalar_required_paths=required,
        declaration_paths=set(),
        download_required_paths=set(),
        expects_download=False,
    )
    assert isinstance(envelope, ProducedStaticReturnEnvelope)
    assert envelope.code.count("return {") == 1
    assert "_envelope_records_0 = [{" in envelope.code
    assert ".append(" not in envelope.code
    produced = workflow_update_module._code_block_produced_output_paths(envelope.code)
    assert required <= produced
    _, violations = workflow_update_module._extraction_code_with_required_static_return(
        envelope.code, required_paths=required
    )
    assert violations == []


def test_producer_mixed_table_and_scalar_single_return_revalidates() -> None:
    plan = _scalar_plan(
        _scalar_binding("output.account_number", "Account Number", ".acct"),
        _table_binding("output.locations[].location", 0),
        _table_binding("output.locations[].status", 1),
    )
    required = {"output.account_number", "output.locations[].location", "output.locations[].status"}
    envelope = produce_covered_static_return_envelope(
        'await page.locator("#lookup").click()\n',
        plan=plan,
        scalar_required_paths=required,
        declaration_paths=set(),
        download_required_paths=set(),
        expects_download=False,
    )
    assert isinstance(envelope, ProducedStaticReturnEnvelope)
    assert envelope.code.count("return {") == 1
    produced = workflow_update_module._code_block_produced_output_paths(envelope.code)
    assert required <= produced
    _, violations = workflow_update_module._extraction_code_with_required_static_return(
        envelope.code, required_paths=required
    )
    assert violations == []


def test_producer_abstains_when_required_table_path_ungrounded() -> None:
    plan = _scalar_plan(_table_binding("output.locations[].location", 0))
    envelope = produce_covered_static_return_envelope(
        'await page.locator("#lookup").click()\n',
        plan=plan,
        scalar_required_paths={"output.locations[].location", "output.locations[].status"},
        declaration_paths=set(),
        download_required_paths=set(),
        expects_download=False,
    )
    assert envelope is None


def test_producer_abstains_on_table_cell_variable_collision() -> None:
    plan = _scalar_plan(_table_binding("output.locations[].location", 0))
    envelope = produce_covered_static_return_envelope(
        '_envelope_cell_0_0_0 = 1\nawait page.locator("#lookup").click()\n',
        plan=plan,
        scalar_required_paths={"output.locations[].location"},
        declaration_paths=set(),
        download_required_paths=set(),
        expects_download=False,
    )
    assert envelope is None


def test_producer_table_with_sibling_declaration_emits_per_row_none_leaf() -> None:
    plan = _scalar_plan(_table_binding("output.locations[].location", 0))
    covered = {"output.locations[].location"}
    declared = {"output.locations[].status"}
    envelope = produce_covered_static_return_envelope(
        'await page.locator("#lookup").click()\n',
        plan=plan,
        scalar_required_paths=covered,
        declaration_paths=declared,
        download_required_paths=set(),
        expects_download=False,
    )
    assert isinstance(envelope, ProducedStaticReturnEnvelope)
    assert envelope.code.count("return {") == 1
    assert '"status": None' in envelope.code
    produced = workflow_update_module._code_block_produced_output_paths(envelope.code)
    assert (covered | declared) <= produced
    _, violations = workflow_update_module._extraction_code_with_required_static_return(
        envelope.code, required_paths=covered, declaration_paths=declared
    )
    assert violations == []


def test_sole_returned_local_binds_to_the_one_required_path_whatever_it_is_named() -> None:
    emitted, violations = workflow_update_module._extraction_code_with_required_static_return(
        'visitors = await page.inner_text(".metric")\nreturn visitors',
        required_paths={"output.visitor_count"},
    )
    assert violations == []
    assert 'return {"output": {"visitor_count": visitors}}' in emitted


def test_a_renamed_local_still_needs_a_single_unambiguous_candidate() -> None:
    _, violations = workflow_update_module._extraction_code_with_required_static_return(
        'visitors = await page.inner_text(".a")\ndates = await page.inner_text(".b")\nreturn visitors',
        required_paths={"output.visitor_count", "output.date_range"},
    )
    assert violations


def test_producer_abstains_on_array_declaration_without_matching_table_group() -> None:
    plan = _scalar_plan(_scalar_binding("output.account_number", "Account Number", ".acct"))
    envelope = produce_covered_static_return_envelope(
        'await page.locator("#lookup").click()\n',
        plan=plan,
        scalar_required_paths={"output.account_number"},
        declaration_paths={"output.locations[].status"},
        download_required_paths=set(),
        expects_download=False,
    )
    assert envelope is None


def test_producer_abstains_on_nested_array_table_path() -> None:
    plan = _scalar_plan(_table_binding("output.groups[].members[].name", 0))
    envelope = produce_covered_static_return_envelope(
        'await page.locator("#lookup").click()\n',
        plan=plan,
        scalar_required_paths={"output.groups[].members[].name"},
        declaration_paths=set(),
        download_required_paths=set(),
        expects_download=False,
    )
    assert envelope is None


def test_produce_split_extraction_envelope_mixed_abstains(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = SimpleNamespace(
        reached_download_target=SimpleNamespace(selector="#download-invoice"),
        scouted_output_covered_paths={"output.statement_amount"},
    )
    monkeypatch.setattr(
        workflow_update_module,
        "download_satisfied_requested_output_paths",
        lambda _ctx: {"output.downloaded_invoice_pdf"},
    )
    result = workflow_update_module._produce_split_extraction_envelope(
        ctx,
        'value = "x"\n',
        required_paths={"output.statement_amount", "output.downloaded_invoice_pdf"},
        declaration_paths=set(),
        label="extract_block",
        signature="sig",
    )
    assert result is None


def _download_classification_ctx(
    *criteria: CompletionCriterion,
    covered_paths: set[str] | None = None,
    download_selector: str | None = "#download-invoice",
) -> SimpleNamespace:
    download = (
        ReachedDownloadTarget(
            selector=download_selector,
            affordance_text="Download Invoice",
            download_kind="attribute",
            source_step="trajectory_recency",
            already_registered=False,
        )
        if download_selector
        else None
    )
    return SimpleNamespace(
        completion_criteria_turn_state=SimpleNamespace(decision=SimpleNamespace(criteria=tuple(criteria))),
        last_code_authoring_repair_context=None,
        scouted_output_covered_paths=set(covered_paths or set()),
        reached_download_target=download,
        flow_evidence=[],
        scout_trajectory=[],
        requested_output_designations=[],
        composition_page_evidence=None,
    )


def test_uncovered_scalar_path_is_not_classified_as_a_download_path() -> None:
    ctx = _download_classification_ctx(
        CompletionCriterion(
            id="statement_amount",
            outcome="The returned record includes the statement amount.",
            output_path="output.statement_amount",
        ),
        covered_paths=set(),
    )

    assert enforcement_module.download_satisfied_requested_output_paths(ctx) == set()
    assert enforcement_module.uncovered_requested_output_paths(ctx) == {"output.statement_amount"}
    assert (
        workflow_update_module._impose_covered_static_return_envelope(
            ctx,
            parsed={},
            workflow_yaml="",
            raw_code_artifact_metadata=[],
            label="download_block",
            target_code=_MIXED_DOWNLOAD_CODE,
            observation_paths={"output.statement_amount"},
            declaration_paths=set(),
            source="scout",
            reason_code="reason",
            signature="sig",
            runtime_attempt_key="key",
        )
        is None
    )


def test_declared_registered_download_kind_on_uncovered_custom_path_is_a_download_path() -> None:
    ctx = _download_classification_ctx(
        CompletionCriterion(
            id="downloaded_invoice_pdf",
            outcome="The returned record includes the downloaded invoice pdf.",
            output_path="output.downloaded_invoice_pdf",
            declared_deliverable_kind="registered_download",
        ),
        covered_paths=set(),
    )

    assert enforcement_module.download_satisfied_requested_output_paths(ctx) == {"output.downloaded_invoice_pdf"}
    assert enforcement_module.uncovered_requested_output_paths(ctx) == set()


def test_declared_registered_download_kind_on_scout_covered_path_is_not_a_download_path() -> None:
    ctx = _download_classification_ctx(
        CompletionCriterion(
            id="npi",
            outcome="The returned record includes NPI.",
            output_path="output.npi",
            declared_deliverable_kind="registered_download",
        ),
        CompletionCriterion(
            id="downloaded_invoice_pdf",
            outcome="The returned record includes the downloaded invoice pdf.",
            output_path="output.downloaded_invoice_pdf",
            declared_deliverable_kind="registered_download",
        ),
        covered_paths={"output.npi"},
    )

    assert enforcement_module.download_satisfied_requested_output_paths(ctx) == {"output.downloaded_invoice_pdf"}


def test_declared_registered_download_kind_survives_requested_output_canonicalization() -> None:
    policy = RequestPolicy(
        completion_criteria=[
            CompletionCriterion(
                id="download",
                outcome="The workflow downloads the invoice file.",
                output_path="output.downloaded_invoice_pdf",
                deliverable_kind="registered_download",
                declared_deliverable_kind="registered_download",
            )
        ]
    )

    request_policy_module._apply_requested_output_completion_criteria(
        policy, "return the downloaded_invoice_pdf when you are done"
    )

    canonical = {criterion.output_path: criterion for criterion in policy.completion_criteria if criterion.output_path}
    criterion = canonical["output.downloaded_invoice_pdf"]
    assert criterion.declared_deliverable_kind == "registered_download"
    assert criterion.deliverable_kind is None


def test_split_seam_declaration_path_is_none_defaulted_not_live_read(monkeypatch: pytest.MonkeyPatch) -> None:
    plan = _scalar_plan(
        _scalar_binding("output.amount", "Amount", ".amt"),
        _scalar_binding("output.optional_note", "Optional Note", ".note"),
    )
    monkeypatch.setattr(workflow_update_module, "download_satisfied_requested_output_paths", lambda _ctx: set())
    monkeypatch.setattr(workflow_update_module, "requested_scalar_output_extraction_plan", lambda _ctx: plan)

    produced = workflow_update_module._produce_split_extraction_envelope(
        SimpleNamespace(),
        'await page.locator("#lookup").click()\n',
        required_paths={"output.amount", "output.optional_note"},
        declaration_paths={"output.optional_note"},
        label="extract_block",
        signature="sig",
    )

    assert produced is not None
    assert '"optional_note": None' in produced
    assert ".note" not in produced
    assert "Optional Note" not in produced


def test_producer_abstains_when_terminal_return_shares_a_line() -> None:
    plan = _scalar_plan(_scalar_binding("output.statement_amount", "Amount Due", ".amt"))
    code = (
        "async with page.expect_download() as dl_info:\n"
        '    await page.locator("#download-invoice").click()\n'
        "dl_info_file = await dl_info.value\n"
        'name = dl_info_file.suggested_filename; return {"downloaded_file_name": name}\n'
    )

    envelope = produce_covered_static_return_envelope(
        code,
        plan=plan,
        scalar_required_paths={"output.statement_amount"},
        declaration_paths=set(),
        download_required_paths={"output.downloaded_invoice_pdf"},
        expects_download=True,
    )

    assert envelope is None


def test_producer_preserves_the_models_download_descriptor_key() -> None:
    plan = _scalar_plan(_scalar_binding("output.statement_amount", "Amount Due", ".amt"))
    code = (
        "async with page.expect_download() as dl_info:\n"
        '    await page.locator("#download-invoice").click()\n'
        'return {"saved_as": dl_info.value.suggested_filename}\n'
    )

    envelope = produce_covered_static_return_envelope(
        code,
        plan=plan,
        scalar_required_paths={"output.statement_amount"},
        declaration_paths=set(),
        download_required_paths={"output.downloaded_invoice_pdf"},
        expects_download=True,
    )

    assert envelope is not None
    assert envelope.code.count("return {") == 1
    assert '"saved_as": dl_info.value.suggested_filename' in envelope.code
    assert "downloaded_file_name" not in envelope.code
    assert {"output.statement_amount"} <= workflow_update_module._code_block_produced_output_paths(envelope.code)


_WITNESS_SELECTOR = "a[href='/statements/100245_2026-05.pdf']"


def _input_templated_provenance() -> dict[str, object]:
    holes = workflow_update_module.input_correspondences_for_interaction(
        {"tool_name": "click", "selector": _WITNESS_SELECTOR},
        {"account_number": "100245", "billing_period": "May 2026"},
    )
    emitted = workflow_update_module.build_input_templated_locator(
        surface="selector", selector=_WITNESS_SELECTOR, role="", name="", holes=holes
    )
    return {
        "source": workflow_update_module.INPUT_TEMPLATED_PROVENANCE_SOURCE,
        "surface": "selector",
        "selector": _WITNESS_SELECTOR,
        "emitted_literal": emitted,
        "holes": [dict(hole) for hole in holes],
    }


def test_input_templated_provenance_admitted() -> None:
    assert workflow_update_module._locator_provenance_is_self_validating(_input_templated_provenance())


def test_input_templated_accessible_name_shape_admitted() -> None:
    holes = workflow_update_module.input_correspondences_for_interaction(
        {"tool_name": "click", "selector": "button", "accessible_name": "Download 100245", "role": "button"},
        {"account_number": "100245"},
    )
    emitted = workflow_update_module.build_input_templated_locator(
        surface="accessible_name", selector="", role="button", name="Download 100245", holes=holes
    )
    record = {
        "source": workflow_update_module.INPUT_TEMPLATED_PROVENANCE_SOURCE,
        "surface": "accessible_name",
        "role": "button",
        "name": "Download 100245",
        "emitted_literal": emitted,
        "holes": [dict(hole) for hole in holes],
    }
    assert workflow_update_module._locator_provenance_is_self_validating(record)


def test_input_templated_provenance_rejects_tamper() -> None:
    tampered_literal = _input_templated_provenance()
    tampered_literal["emitted_literal"] = str(tampered_literal["emitted_literal"]).replace("account_number", "attacker")
    assert not workflow_update_module._locator_provenance_is_self_validating(tampered_literal)

    reordered = _input_templated_provenance()
    reordered["holes"] = list(reversed(reordered["holes"]))  # type: ignore[arg-type]
    assert not workflow_update_module._locator_provenance_is_self_validating(reordered)

    missing_holes = _input_templated_provenance()
    missing_holes["holes"] = []
    assert not workflow_update_module._locator_provenance_is_self_validating(missing_holes)


def test_input_templated_provenance_rejects_dropping_distinct_iso_projection() -> None:
    selector = 'a[data-date="2026-05-01"][href="/statements/2026-05.pdf"]'
    holes = workflow_update_module.input_correspondences_for_interaction(
        {"tool_name": "click", "selector": selector},
        {"billing_start_date": "2026-05-01"},
    )
    emitted = workflow_update_module.build_input_templated_locator(
        surface="selector", selector=selector, role="", name="", holes=holes
    )
    provenance = {
        "source": workflow_update_module.INPUT_TEMPLATED_PROVENANCE_SOURCE,
        "surface": "selector",
        "selector": selector,
        "emitted_literal": emitted,
        "holes": [dict(hole) for hole in holes],
    }
    assert workflow_update_module._locator_provenance_is_self_validating(provenance)

    tampered = dict(provenance)
    tampered["holes"] = [dict(holes[0])]
    assert not workflow_update_module._locator_provenance_is_self_validating(tampered)


def test_dynamic_selection_row_input_templated_provenance_recomputes_and_rejects_capture_tamper() -> None:
    selector = "div.statement-row >> nth=2"
    row_evidence = {
        "source_url": "https://example.com/statements",
        "target_selector": selector,
        "row_selector": "div.statement-row",
        "row_text": "Statement May 5, 2026",
        "row_selector_count": 4,
        "row_text_match_count": 1,
        "period_matches": [
            {"period": "2026-05", "selected_row_match_count": 1, "row_match_count": 1},
        ],
        "selected_index": 2,
    }
    row_evidence["evidence_fingerprint"] = code_block_synthesis_module.dynamic_row_evidence_fingerprint(**row_evidence)
    interaction = {
        "tool_name": "click",
        "selector": selector,
        "source_url": "https://example.com/statements",
        "dynamic_row_evidence": row_evidence,
    }
    interaction["input_correspondences"] = workflow_update_module.input_correspondences_for_interaction(
        interaction,
        {"download_start_date": "2026-05-01", "download_end_date": "2026-05-31"},
    )
    synthesized = code_block_synthesis_module.synthesize_code_block([interaction], strict_selectors=True)
    assert synthesized is not None
    provenance = next(
        record for record in synthesized.diagnostics.locator_provenance if record.get("surface") == "row_text"
    )
    assert workflow_update_module._locator_provenance_is_self_validating(provenance)

    tampered = dict(provenance)
    tampered["row_text_match_count"] = 2
    assert not workflow_update_module._locator_provenance_is_self_validating(tampered)

    tampered_period = json.loads(json.dumps(provenance))
    tampered_period["period_matches"][0]["row_match_count"] = 2
    tampered_period["evidence_fingerprint"] = code_block_synthesis_module.dynamic_row_evidence_fingerprint(
        source_url=tampered_period["source_url"],
        target_selector=tampered_period["target_selector"],
        row_selector=tampered_period["row_selector"],
        row_text=tampered_period["row_text"],
        row_selector_count=tampered_period["row_selector_count"],
        row_text_match_count=tampered_period["row_text_match_count"],
        period_matches=tampered_period["period_matches"],
        selected_index=tampered_period["selected_index"],
    )
    assert not workflow_update_module._locator_provenance_is_self_validating(tampered_period)


def test_dynamic_selection_public_provenance_omits_page_text_query_and_credentials() -> None:
    provenance = {
        "trajectory_index": 2,
        "source": workflow_update_module.INPUT_TEMPLATED_PROVENANCE_SOURCE,
        "surface": "row_text",
        "source_url": "https://user:password@example.com/statements?token=secret#private",
        "target_selector": "div.row >> nth=2",
        "row_selector": "div.row",
        "row_text": "Customer Alice secret account text May 5, 2026",
        "row_selector_count": 4,
        "row_text_match_count": 1,
        "period_matches": [
            {"period": "2026-05", "selected_row_match_count": 1, "row_match_count": 1},
        ],
        "selected_index": 2,
        "emitted_literal": "page.locator('div.row')",
        "holes": [
            {
                "input_key": "download_start_date",
                "matched_literal": "2026-05",
                "parameter_value": "2026-05-01",
                "transform": "iso_date_to_year_month",
                "position": 35,
            }
        ],
    }

    public = workflow_update_module._public_locator_provenance([provenance])

    serialized = json.dumps(public)
    assert public == [
        {
            "trajectory_index": 2,
            "source": workflow_update_module.INPUT_TEMPLATED_PROVENANCE_SOURCE,
            "surface": "row_text",
            "source_origin": "https://example.com",
            "input_keys": ["download_start_date"],
            "transforms": ["iso_date_to_year_month"],
        }
    ]
    for secret in ("Alice", "secret", "password", "token", "2026-05-01"):
        assert secret not in serialized

    tampered = dict(provenance)
    tampered["source_url"] = "https://other.example.org/statements"
    assert not workflow_update_module._locator_provenance_is_self_validating(tampered)


def test_confluence_stamps_and_clears_input_correspondences() -> None:
    ctx = _code_only_ctx()
    ctx.scout_trajectory = [
        {
            "tool_name": "click",
            "selector": _WITNESS_SELECTOR,
            "source_url": "https://example.com/s",
            "trajectory_index": 0,
        }
    ]
    yaml_with_params = textwrap.dedent(
        """
        workflow_definition:
          parameters:
            - parameter_type: workflow
              workflow_parameter_type: string
              key: account_number
              default_value: "100245"
            - parameter_type: workflow
              workflow_parameter_type: string
              key: billing_period
              default_value: "May 2026"
          blocks: []
        """
    )
    workflow_update_module._enrich_scout_trajectory_input_correspondences(yaml_with_params, ctx)
    stamped = ctx.scout_trajectory[0].get("input_correspondences")
    assert stamped is not None
    assert {c["input_key"] for c in stamped} == {"account_number", "billing_period"}

    yaml_without_params = "workflow_definition:\n  parameters: []\n  blocks: []\n"
    workflow_update_module._enrich_scout_trajectory_input_correspondences(yaml_without_params, ctx)
    assert "input_correspondences" not in ctx.scout_trajectory[0]


def test_reconcile_scout_interaction_positional_map_skips_witness_rows() -> None:
    scout_trajectory = [
        {"tool_name": "type_text", "selector": "#account", "typed_length": 6},
        {"tool_name": "type_text", "selector": "#period", "typed_length": 8},
    ]
    synthesized_parameters = [
        {"key": "account_number", "default_value": "100245", "source": "locator_witness"},
        {"key": "account_field"},
        {"key": "period_field"},
    ]
    account = workflow_update_module._scout_interaction_for_synthesized_parameter(
        synthesized_key="account_field",
        scout_trajectory=scout_trajectory,
        synthesized_parameters=synthesized_parameters,
    )
    period = workflow_update_module._scout_interaction_for_synthesized_parameter(
        synthesized_key="period_field",
        scout_trajectory=scout_trajectory,
        synthesized_parameters=synthesized_parameters,
    )
    assert account is not None and account["selector"] == "#account"
    assert period is not None and period["selector"] == "#period"


class TestSynthesizedParameterMultiFillReconciliation:
    def _reconcile(
        self,
        *,
        submitted_code: str,
        synthesized_parameters: list[dict[str, str]],
        scout_trajectory: list[dict[str, object]],
    ) -> tuple[dict[str, object], object]:
        parsed: dict[str, object] = {"workflow_definition": {"parameters": [], "blocks": []}}
        code_block: dict[str, object] = {"label": "search_directory", "code": submitted_code}
        reconciliation = workflow_update_module._reconcile_synthesized_parameters(
            parsed=parsed,
            code_block=code_block,
            submitted_code=submitted_code,
            synthesized_parameters=synthesized_parameters,
            scout_trajectory=scout_trajectory,
        )
        return parsed, reconciliation

    def test_divergent_names_auto_declare_reusable_required_rows(self) -> None:
        parsed, reconciliation = self._reconcile(
            submitted_code='await page.locator("#submit").click()',
            synthesized_parameters=[
                {"key": "address_city_county_or_zip_code"},
                {"key": "provider_specialty"},
            ],
            scout_trajectory=[
                {"tool_name": "type_text", "selector": "#location", "typed_length": 7},
                {"tool_name": "type_text", "selector": "#specialty", "typed_length": 10},
            ],
        )

        assert reconciliation.violations == []
        assert reconciliation.repair_context is None
        assert reconciliation.parameter_keys == ["address_city_county_or_zip_code", "provider_specialty"]
        assert parsed["workflow_definition"]["parameters"] == [
            {
                "parameter_type": "workflow",
                "workflow_parameter_type": "string",
                "key": "address_city_county_or_zip_code",
            },
            {"parameter_type": "workflow", "workflow_parameter_type": "string", "key": "provider_specialty"},
        ]

    def test_duplicate_key_multi_fill_still_fails_closed(self) -> None:
        parsed, reconciliation = self._reconcile(
            submitted_code='await page.locator("#submit").click()',
            synthesized_parameters=[
                {"key": "address_city_county_or_zip_code"},
                {"key": "address_city_county_or_zip_code"},
            ],
            scout_trajectory=[
                {"tool_name": "type_text", "selector": "#location", "typed_length": 7},
                {"tool_name": "type_text", "selector": "#location_again", "typed_length": 7},
            ],
        )

        assert any("literal binding is ambiguous" in violation for violation in reconciliation.violations)
        assert parsed["workflow_definition"]["parameters"] == []
        assert reconciliation.repair_context is not None
        assert reconciliation.repair_context.reason_code == "synthesized_parameter_binding_ambiguous"

    def _directory_output_intent_yaml(self) -> str:
        return _yaml(
            """
            title: Directory lookup
            workflow_definition:
              parameters:
              - {parameter_type: output, key: directory_result}
              blocks:
              - block_type: code
                label: search_directory
                prompt: Search the directory and return structured provider result data.
                code: |
                  await page.locator("#search").click()
            """
        )

    def test_co_computed_metadata_contract_surfaces_when_output_contract_deficient(self) -> None:
        ctx = _code_only_ctx()
        ctx.request_policy = RequestPolicy(
            completion_criteria=[
                CompletionCriterion(id="c1", outcome="return the provider npi", output_path="provider.npi")
            ]
        )

        contract = workflow_update_module._co_computed_metadata_repair_contract(
            ctx, self._directory_output_intent_yaml(), None
        )

        assert contract is not None
        assert contract["block_label"] == "search_directory"
        assert any(path == "provider.npi" for path in contract["required_goal_value_paths"])

    def test_co_computed_metadata_contract_is_none_without_output_intent(self) -> None:
        ctx = _code_only_ctx()
        ctx.request_policy = RequestPolicy(
            completion_criteria=[
                CompletionCriterion(id="c1", outcome="return the provider npi", output_path="provider.npi")
            ]
        )
        workflow_yaml = _yaml(
            """
            title: Directory lookup
            workflow_definition:
              blocks:
              - block_type: code
                label: open_directory
                code: |
                  await page.goto("https://example.com/directory")
            """
        )

        contract = workflow_update_module._co_computed_metadata_repair_contract(ctx, workflow_yaml, None)

        assert contract is None

    def test_co_computed_metadata_contract_labels_output_intent_block_in_multi_block(self) -> None:
        # In a multi-block workflow the metadata-missing block is not necessarily the imposed carrier;
        # the repair hint must point at the block that actually owns the output, derived from the yaml.
        ctx = _code_only_ctx()
        ctx.request_policy = RequestPolicy(
            completion_criteria=[
                CompletionCriterion(id="c1", outcome="return the provider npi", output_path="provider.npi")
            ]
        )
        workflow_yaml = _yaml(
            """
            title: Directory lookup
            workflow_definition:
              parameters:
              - {parameter_type: output, key: directory_result}
              blocks:
              - block_type: code
                label: open_directory
                code: |
                  await page.goto("https://example.com/directory")
              - block_type: code
                label: read_provider
                prompt: Search the directory and return structured provider result data.
                code: |
                  npi = (await page.locator("#npi").inner_text()).strip()
                  return {"provider": {"npi": npi}}
            """
        )

        contract = workflow_update_module._co_computed_metadata_repair_contract(ctx, workflow_yaml, None)

        assert contract is not None
        assert contract["block_label"] == "read_provider"

    def _owned_carrier_ctx(self) -> CopilotContext:
        ctx = _code_only_ctx()
        ctx.spine_imposition_owned_attempt = True
        ctx.spine_imposition_carrier_label = "search_directory"
        return ctx

    def _carrier_yaml(self, code: str) -> str:
        return _yaml(
            f"""
            title: Directory lookup
            workflow_definition:
              parameters:
              - {{parameter_type: output, key: directory_result}}
              blocks:
              - block_type: code
                label: search_directory
                prompt: Search the directory and return structured provider result data.
                code: |
                  {code}
            """
        )

    def _divergent_multi_fill_scout(self) -> list[dict[str, object]]:
        return [
            {
                "tool_name": "type_text",
                "selector": "#location",
                "source_url": "https://example.com/directory",
                "typed_length": 7,
                "role": "textbox",
                "accessible_name": "Address City County or Zip Code",
                "trajectory_index": 0,
            },
            {
                "tool_name": "type_text",
                "selector": "#specialty",
                "source_url": "https://example.com/directory",
                "typed_length": 10,
                "role": "textbox",
                "accessible_name": "Provider Specialty",
                "trajectory_index": 1,
            },
            {
                "tool_name": "click",
                "selector": "#search",
                "source_url": "https://example.com/directory",
                "role": "button",
                "accessible_name": "Search",
                "trajectory_index": 2,
            },
        ]

    def _divergent_multi_fill_yaml(self, *, keyed_return: bool) -> str:
        tail = (
            '\n                  npi = (await page.locator("#npi").inner_text()).strip()'
            '\n                  return {"provider": {"npi": npi}}'
            if keyed_return
            else ""
        )
        return _yaml(
            f"""
            title: Directory lookup
            workflow_definition:
              parameters:
              - {{parameter_type: output, key: directory_result}}
              blocks:
              - block_type: code
                label: search_directory
                prompt: Search the directory and return structured provider result data.
                code: |
                  await page.locator("#location").fill("Raleigh")
                  await page.locator("#specialty").fill("Cardiology")
                  await page.locator("#search").click(){tail}
            """
        )


def _goal_reaching_freehand_ctx(*, credential: bool = False) -> CopilotContext:
    ctx = _code_only_ctx()
    _enable_imposition(ctx)
    if credential:
        ctx.scout_trajectory = [
            {
                "tool_name": "fill_credential_field",
                "selector": "#username",
                "source_url": "https://example.com/login",
                "credential_id": "cred_1",
                "credential_name": "portal",
                "credential_field": "username",
                "trajectory_index": 0,
            },
            {
                "tool_name": "click",
                "selector": "#signin",
                "source_url": "https://example.com/login",
                "role": "button",
                "accessible_name": "Sign In",
                "trajectory_index": 1,
            },
        ]
    else:
        ctx.scout_trajectory = [
            {
                "tool_name": "type_text",
                "selector": "#search",
                "source_url": "https://example.com/find",
                "typed_length": 5,
                "role": "textbox",
                "accessible_name": "Search",
                "trajectory_index": 0,
            },
            {
                "tool_name": "click",
                "selector": "button",
                "source_url": "https://example.com/find",
                "role": "button",
                "accessible_name": "Sign In",
                "trajectory_index": 1,
            },
        ]
    return ctx


def _freehand_block_yaml(code: str, *, label: str = "find_address", parameters: str = "") -> str:
    indented = "\n".join(f"          {line}" for line in code.splitlines())
    return (
        "title: t\n"
        "workflow_definition:\n"
        f"{parameters}"
        "  blocks:\n"
        "  - block_type: code\n"
        f"    label: {label}\n"
        "    code: |\n"
        f"{indented}\n"
    )


_RENDER_PREFLIGHT_FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "copilot" / "wr_jinja_unrenderable_proposed_workflow.json"
)

_PARAMETERS_NAMESPACE_CODE_YAML = _yaml(
    """
    title: Utility service request
    workflow_definition:
      parameters:
      - {parameter_type: workflow, workflow_parameter_type: string, key: business_name, default_value: Sample Business}
      blocks:
      - block_type: code
        label: submit_request
        parameter_keys: [business_name]
        code: |
          # Workflow input bindings: {{ parameters.business_name }}
          await page.goto("https://example.com/request")
          await page.locator("#company").fill(str(business_name).strip())
    """
)

_UNDECLARED_ROOT_CODE_YAML = _yaml(
    """
    title: Utility service request
    workflow_definition:
      blocks:
      - block_type: code
        label: submit_request
        code: |
          await page.goto("https://example.com/request")
          await page.locator("#company").fill("{{ frobnicator }}")
    """
)

_TEMPLATE_SYNTAX_ERROR_CODE_YAML = _yaml(
    """
    title: Utility service request
    workflow_definition:
      parameters:
      - {parameter_type: workflow, workflow_parameter_type: string, key: business_name, default_value: Sample Business}
      blocks:
      - block_type: code
        label: submit_request
        parameter_keys: [business_name]
        code: |
          await page.goto("https://example.com/request")
          await page.locator("#company").fill("{{ business_name")
    """
)

_RENDERABLE_JINJA_CODE_YAML = _yaml(
    """
    title: Utility service request
    workflow_definition:
      parameters:
      - {parameter_type: workflow, workflow_parameter_type: string, key: business_name, default_value: Sample Business}
      blocks:
      - block_type: code
        label: submit_request
        parameter_keys: [business_name]
        code: |
          await page.goto("https://example.com/request")
          await page.locator("#company").fill("{{ business_name }}")
    """
)


class TestCodeBlockRenderPreflightSeam:
    @pytest.mark.asyncio
    async def test_code_block_preflight_renderable_draft_persists(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        result = await _update_workflow({"workflow_yaml": _RENDERABLE_JINJA_CODE_YAML}, ctx)
        assert result["ok"] is True
        assert "{{ business_name }}" in ctx.workflow_yaml

    @pytest.mark.asyncio
    async def test_code_block_preflight_renderable_draft_with_envelope_imposition_persists(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        ctx.request_policy = RequestPolicy(
            completion_criteria=[
                _typed_completion_criterion(
                    id="requested_value",
                    output_path="output.record_id",
                    level="run",
                    method_mandated=False,
                    kind="outcome",
                )
            ]
        )
        workflow_yaml = _yaml(
            """
            title: Entry lookup
            workflow_definition:
              parameters:
              - {parameter_type: workflow, workflow_parameter_type: string, key: business_name, default_value: Sample Business}
              blocks:
              - block_type: code
                label: extract_entry_output
                parameter_keys: [business_name]
                code: |
                  record_id = "{{ business_name }}"
            """
        )
        result = await _update_workflow({"workflow_yaml": workflow_yaml}, ctx, allow_missing_credentials=True)
        assert result["ok"] is True
        parsed = parse_workflow_yaml(ctx.workflow_yaml)
        assert isinstance(parsed, dict)
        code = str(_single_code_block(parsed)["code"])
        assert "{{ business_name }}" in code
        assert 'return {"output": {"record_id": record_id}}' in code

    @pytest.mark.asyncio
    async def test_code_block_preflight_unchanged_legacy_block_is_not_rerejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        ctx.workflow_yaml = _PARAMETERS_NAMESPACE_CODE_YAML
        result = await _update_workflow({"workflow_yaml": _PARAMETERS_NAMESPACE_CODE_YAML}, ctx)
        assert (result.get("data") or {}).get("reason_code") != "code_block_unrenderable"
        assert result["ok"] is True


def _under_scouted_order_status_ctx() -> CopilotContext:
    ctx = _code_only_ctx()
    _enable_imposition(ctx)
    ctx.scout_trajectory = [
        {
            "tool_name": "type_text",
            "selector": "#confirmation",
            "source_url": "https://portal.example.com/order-status",
            "role": "textbox",
            "accessible_name": "Confirmation number",
            "typed_length": 8,
            "trajectory_index": 0,
        }
    ]
    assert enforcement_module.synthesized_trajectory_reaches_goal(ctx) is False
    return ctx


def _pre_goal_wizard_ctx() -> CopilotContext:
    ctx = _code_only_ctx()
    _enable_imposition(ctx)
    ctx.scout_trajectory = [
        {
            "tool_name": "fill_credential_field",
            "selector": "#username",
            "source_url": "https://utility.example.com/login",
            "credential_id": "cred_1",
            "credential_name": "portal",
            "credential_field": "username",
            "trajectory_index": 0,
        },
        {
            "tool_name": "fill_credential_field",
            "selector": "#password",
            "source_url": "https://utility.example.com/login",
            "credential_id": "cred_1",
            "credential_name": "portal",
            "credential_field": "password",
            "trajectory_index": 1,
        },
        {
            "tool_name": "click",
            "selector": "#sign-in",
            "source_url": "https://utility.example.com/login",
            "role": "button",
            "accessible_name": "Sign In",
            "trajectory_index": 2,
        },
        {
            "tool_name": "click",
            "selector": "#business-toggle",
            "source_url": "https://utility.example.com/start-service",
            "role": "button",
            "accessible_name": "Business",
            "trajectory_index": 3,
        },
        {
            "tool_name": "type_text",
            "selector": "#service-address",
            "source_url": "https://utility.example.com/start-service",
            "role": "textbox",
            "accessible_name": "Service address",
            "typed_length": 20,
            "trajectory_index": 4,
        },
    ]
    assert enforcement_module.synthesized_trajectory_reaches_goal(ctx) is False
    return ctx


def _wizard_block_yaml(*, include_business_block: bool) -> str:
    sign_in_code = (
        '          _scout_entry_target = page.locator("#username")\n'
        "          if await _scout_entry_target.count() == 1:\n"
        '              await page.locator("#username").fill(portal.username)\n'
        '              await page.locator("#password").fill(portal.password)\n'
        '              await page.locator("#sign-in").click()\n'
    )
    business_code = (
        '          await page.locator("#business-toggle").click()\n'
        '          await page.locator("#service-address").fill(str(service_address))\n'
    )
    yaml_text = (
        "title: t\n"
        "workflow_definition:\n"
        "  parameters:\n"
        "  - {parameter_type: credential, key: portal, credential_id: cred_1}\n"
        "  - {parameter_type: workflow, workflow_parameter_type: string, key: service_address, default_value: addr}\n"
        "  blocks:\n"
        "  - block_type: code\n"
        "    label: sign_in_to_portal\n"
        "    code: |\n"
        f"{sign_in_code}"
    )
    if include_business_block:
        yaml_text += "  - block_type: code\n    label: open_business_start_service\n    code: |\n" + business_code
    return yaml_text


class TestPreGoalWizardSpineReplay:
    def test_sign_in_only_collapse_rejected_naming_dropped_rungs(self) -> None:
        ctx = _pre_goal_wizard_ctx()
        result = workflow_update_module._pre_persist_scouted_spine_result(
            _wizard_block_yaml(include_business_block=False), ctx
        )
        assert result is not None
        assert result.repair_context is not None
        assert result.repair_context.reason_code == "scouted_spine_under_build"
        assert "#business-toggle" in result.violations[0]
        assert "#service-address" in result.violations[0]

    def test_multi_block_draft_covering_all_scouted_rungs_admitted(self) -> None:
        ctx = _pre_goal_wizard_ctx()
        result = workflow_update_module._pre_persist_scouted_spine_result(
            _wizard_block_yaml(include_business_block=True), ctx
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_multi_block_covering_draft_persists_with_multiple_blocks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)

        async def _fake_process_from_yaml(**kwargs: object) -> SimpleNamespace:
            parsed = yaml.safe_load(str(kwargs["workflow_yaml"]))
            blocks = [SimpleNamespace(label=block.get("label")) for block in parsed["workflow_definition"]["blocks"]]
            return SimpleNamespace(workflow_definition=SimpleNamespace(blocks=blocks), proxy_location=None)

        monkeypatch.setattr(workflow_update_module, "_process_workflow_yaml", _fake_process_from_yaml)
        ctx = _code_only_ctx()
        _enable_imposition(ctx)
        ctx.scout_trajectory = [
            {
                "tool_name": "click",
                "selector": "#business-toggle",
                "source_url": "https://utility.example.com/start-service",
                "role": "button",
                "accessible_name": "Business",
                "trajectory_index": 0,
            },
            {
                "tool_name": "type_text",
                "selector": "#service-address",
                "source_url": "https://utility.example.com/start-service",
                "role": "textbox",
                "accessible_name": "Service address",
                "typed_length": 20,
                "trajectory_index": 1,
            },
        ]
        submitted = (
            "title: t\n"
            "workflow_definition:\n"
            "  parameters:\n"
            "  - {parameter_type: workflow, workflow_parameter_type: string, key: service_address, default_value: a}\n"
            "  blocks:\n"
            "  - block_type: code\n"
            "    label: open_business_services\n"
            "    code: |\n"
            '      await page.locator("#business-toggle").click()\n'
            "  - block_type: code\n"
            "    label: fill_service_address\n"
            "    code: |\n"
            '      await page.locator("#service-address").fill(str(service_address))\n'
        )
        ctx.workflow_yaml = submitted

        result = await _update_workflow({"workflow_yaml": submitted}, ctx)

        assert result["ok"] is True
        assert result["data"]["block_count"] == 2


class TestPrePersistScoutedSpineGate:
    def test_covering_draft_returns_none(self) -> None:
        ctx = _code_only_ctx()
        _enable_imposition(ctx)
        code = 'await page.locator("#search-submit").click()'
        assert workflow_update_module._pre_persist_scouted_spine_result(_freehand_block_yaml(code), ctx) is None

    def test_draft_dropping_scouted_rung_rejected_with_rung_provenance(self) -> None:
        ctx = _code_only_ctx()
        _enable_imposition(ctx)
        code = 'print(await page.locator("body").inner_text())'
        result = workflow_update_module._pre_persist_scouted_spine_result(_freehand_block_yaml(code), ctx)
        assert result is not None
        assert result.repair_context is not None
        assert result.repair_context.reason_code == "scouted_spine_under_build"
        assert "#search-submit" in result.violations[0]

    def test_gate_skipped_without_imposition(self) -> None:
        ctx = _code_only_ctx()
        code = 'print(await page.locator("body").inner_text())'
        assert workflow_update_module._pre_persist_scouted_spine_result(_freehand_block_yaml(code), ctx) is None

    def test_gate_skipped_without_scout_trajectory(self) -> None:
        ctx = _code_only_ctx()
        _enable_imposition(ctx)
        ctx.scout_trajectory = []
        code = 'print(await page.locator("body").inner_text())'
        assert workflow_update_module._pre_persist_scouted_spine_result(_freehand_block_yaml(code), ctx) is None

    def test_gate_reuses_imposition_synthesized_result(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx = _code_only_ctx()
        _enable_imposition(ctx)
        ctx.imposition_synthesized_block = code_block_synthesis_module.synthesize_code_block(
            ctx.scout_trajectory, strict_selectors=True, reached_download_target=None
        )
        assert ctx.imposition_synthesized_block is not None

        def _fail_resynthesis(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("pre-persist gate re-synthesized instead of reusing the imposition result")

        monkeypatch.setattr(workflow_update_module, "synthesize_code_block", _fail_resynthesis)
        code = 'await page.locator("#search-submit").click()'
        assert workflow_update_module._pre_persist_scouted_spine_result(_freehand_block_yaml(code), ctx) is None

    def test_gate_declines_synthesizer_side_partition_findings(self) -> None:
        ctx = _code_only_ctx()
        _enable_imposition(ctx)
        ctx.scout_trajectory = [
            {
                "tool_name": "click",
                "selector": "#stage-a",
                "source_url": "https://example.com/records",
                "trajectory_index": 0,
            },
            {"tool_name": "press_key", "key": "", "trajectory_index": 1},
            {"tool_name": "click", "selector": "#stage-b", "trajectory_index": 2},
        ]
        synthesized = code_block_synthesis_module.synthesize_code_block(ctx.scout_trajectory, strict_selectors=True)
        assert synthesized is not None
        covering_code = synthesized.interaction_code or synthesized.code
        draft_calls = [
            (mutation.method, mutation.receiver)
            for mutation in workflow_update_module._browser_surface_for_code(covering_code)[0]
        ]
        findings = code_block_synthesis_module.spine_partition_findings(
            synthesized.diagnostics, draft_calls, ctx.scout_trajectory
        )
        assert any(finding.kind == code_block_synthesis_module.UNFORGIVEN_DROP_FINDING for finding in findings)

        result = workflow_update_module._pre_persist_scouted_spine_result(_freehand_block_yaml(covering_code), ctx)

        assert result is None
        assert ctx.code_authoring_guardrail_reject_count == 0

    @pytest.mark.asyncio
    async def test_covering_draft_persists_through_canonical_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_successful_update(monkeypatch)
        monkeypatch.setattr(workflow_update_module, "_workflow_requires_canonical_persist", lambda *_a, **_k: True)
        canonical_writes: list[dict[str, object]] = []

        async def _spy_update_workflow_definition(**kwargs: object) -> None:
            canonical_writes.append(kwargs)

        async def _fake_created_by_stamp(*_args: object, **_kwargs: object) -> str | None:
            return "copilot"

        async def _fake_canonical_workflow(**_kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(
                workflow_definition=SimpleNamespace(blocks=[SimpleNamespace(label="submit_search")]),
                title="t",
                description=None,
                proxy_location=None,
                webhook_callback_url=None,
                totp_verification_url=None,
                totp_identifier=None,
                persist_browser_session=False,
                pin_saved_session_ip=False,
                browser_profile_id=None,
                browser_profile_key=None,
                model=None,
                max_screenshot_scrolls=None,
                extra_http_headers=None,
                cdp_connect_headers=None,
                run_with=None,
                ai_fallback=True,
                cache_key=None,
                adaptive_caching=None,
                enable_self_healing=False,
                code_version=None,
                run_sequentially=False,
                sequential_key=None,
            )

        monkeypatch.setattr(
            workflow_update_module.app,
            "WORKFLOW_SERVICE",
            SimpleNamespace(update_workflow_definition=_spy_update_workflow_definition),
        )
        monkeypatch.setattr(workflow_update_module, "resolve_copilot_created_by_stamp", _fake_created_by_stamp)
        monkeypatch.setattr(workflow_update_module, "_process_workflow_yaml", _fake_canonical_workflow)
        ctx = _code_only_ctx()
        _enable_imposition(ctx)
        submitted = _freehand_block_yaml('await page.locator("#search-submit").click()', label="submit_search")
        ctx.workflow_yaml = submitted

        result = await _update_workflow({"workflow_yaml": submitted}, ctx)

        assert result["ok"] is True
        assert len(canonical_writes) == 1
        assert ctx.persisted_draft_browser_calls == [("click", "page.locator('#search-submit')")]


_P6_UNDER_SCOUT_RECORDED_RUN_IDS = (
    "wr_551756368310299524",
    "wr_551757699750161408",
    "wr_551759572355902654",
)
_P10_SPINE_COLLAPSE_RECORDED_RUN_ID = "wr_551770266824468500"

# Selector shapes are transcribed verbatim from each recorded run's proposed workflow; only the mock
# host is placeholder-normalized per this file's OSS-synced target convention.
_P6_RECORDED_FREEHAND_BLOCKS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "wr_551756368310299524": (
        "lookup_order",
        'await page.goto("https://example.com/order-status")\n'
        'await page.locator("#confirmation").fill(str(enter_confirmation))\n'
        "await page.locator('button[data-action=\"orderLookup\"]').click()\n"
        'await page.locator(\'button[data-action="orderDocuments"]\').wait_for(state="visible", timeout=10000)\n'
        "await page.locator('button[data-action=\"orderDocuments\"]').click()\n",
        ("orderLookup", "orderDocuments"),
    ),
    "wr_551757699750161408": (
        "lookup_order",
        'await page.goto("https://example.com/order-status")\n'
        'await page.locator("#confirmation").fill(str(enter_confirmation))\n'
        "await page.locator('button[data-action=\"orderLookup\"]').click()\n",
        ("orderLookup",),
    ),
    "wr_551759572355902654": (
        "retrieve_resale_document",
        "documents_button = page.locator('button[data-action=\"orderDocuments\"]')\n"
        "await documents_button.click()\n"
        'rows = page.locator("tr")\n',
        ("documents_button",),
    ),
}


class TestRecordedGauntletPacketReplay:
    @pytest.mark.parametrize("run_id", [_P10_SPINE_COLLAPSE_RECORDED_RUN_ID])
    def test_p10_recorded_sign_in_only_collapse_rejected_naming_dropped_rungs(self, run_id: str) -> None:
        ctx = _pre_goal_wizard_ctx()
        result = workflow_update_module._pre_persist_scouted_spine_result(
            _wizard_block_yaml(include_business_block=False), ctx
        )
        assert result is not None
        assert result.repair_context is not None
        assert result.repair_context.reason_code == "scouted_spine_under_build"
        assert "#business-toggle" in result.violations[0]
        assert "#service-address" in result.violations[0]

    @pytest.mark.parametrize("run_id", [_P10_SPINE_COLLAPSE_RECORDED_RUN_ID])
    def test_p10_recorded_covering_multi_block_spine_admitted(self, run_id: str) -> None:
        ctx = _pre_goal_wizard_ctx()
        result = workflow_update_module._pre_persist_scouted_spine_result(
            _wizard_block_yaml(include_business_block=True), ctx
        )
        assert result is None


_P3_PACKET_FIXTURE = json.loads(
    (Path(__file__).parent / "copilot" / "fixtures" / "p3_definition_contract_reject_packets.json").read_text()
)
_P3_PACKETS_BY_RUN = {run["run"]: run for run in _P3_PACKET_FIXTURE["runs"]}
_P3_REPLAY_BINDINGS: dict[str, dict[str, tuple[str, str]]] = {
    "run1": {
        "account_number": ("#accountNumberFilter", "100245"),
        "billing_start_date": ("#billingPeriodStart", "2026-05-01"),
        "billing_end_date": ("#billingPeriodEnd", "2026-05-31"),
    },
    "run2": {
        "account_number": ("#accountNumberFilter", "100245"),
        "billing_period_start": ("#billingPeriodStart", "2026-05-01"),
        "billing_period_end": ("#billingPeriodEnd", "2026-05-31"),
    },
    "run3": {
        "account_number": ("#accountNumberFilter", "100245"),
        "billing_start_date": ("#billingPeriodStart", "2026-05-01"),
        "billing_end_date": ("#billingPeriodEnd", "2026-05-31"),
    },
}


def _p3_definition_ctx() -> CopilotContext:
    ctx = _code_only_ctx()
    ctx.request_policy = RequestPolicy(
        completion_criteria=[
            CompletionCriterion(
                id="c0",
                outcome="The workflow uses the account number and the billing period dates as reusable inputs.",
                level="definition",
                output_path="workflow.parameters",
            )
        ]
    )
    return ctx


def _p3_recorded_clicks(packet: dict[str, Any]) -> list[dict[str, Any]]:
    return [interaction for interaction in packet["scouted_interactions"] if interaction["tool_name"] == "click"]


def _p3_draft_yaml(packet: dict[str, Any], declared_keys: Iterable[str]) -> str:
    code = "".join(
        f'await page.locator("{interaction["selector"]}").click()\n' for interaction in _p3_recorded_clicks(packet)
    )
    return yaml.safe_dump(
        {
            "title": "Download matching statement invoice",
            "workflow_definition": {
                "parameters": [
                    {"parameter_type": "workflow", "key": key, "workflow_parameter_type": "string"}
                    for key in sorted(declared_keys)
                ],
                "blocks": [
                    {
                        "block_type": "code",
                        "label": packet["block_labels"][0],
                        "parameter_keys": [],
                        "code": code,
                    }
                ],
            },
        },
        sort_keys=False,
    )


def _p3_scout_trajectory(packet: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "tool_name": interaction["tool_name"],
            "selector": interaction["selector"],
            "source_url": interaction["source_url"],
            "trajectory_index": interaction["trajectory_index"],
        }
        for interaction in _p3_recorded_clicks(packet)
    ]


def _p3_composition_evidence(
    packet: dict[str, Any],
    bindings: dict[str, tuple[str, str]],
    *,
    extra_fields: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    clicks = _p3_recorded_clicks(packet)
    return {
        "source_tool": "inspect_page_for_composition",
        "current_url": clicks[0]["source_url"],
        "forms": [
            {
                "fields": [{"selector": selector, "value": value} for selector, value in bindings.values()]
                + (extra_fields or []),
                "submit_controls": [{"selector": clicks[-1]["selector"]}],
            }
        ],
    }


def _recorded_summary_prefix(recorded_summary: str) -> str:
    return recorded_summary[:-3] if recorded_summary.endswith("...") else recorded_summary


class TestDefinitionContractRecordedPacketReplay:
    @pytest.mark.parametrize("run_name", ["run1", "run2", "run3"])
    def test_recorded_packets_reproduce_definition_plane_reject_byte_prefix(self, run_name: str) -> None:
        packet = _P3_PACKETS_BY_RUN[run_name]
        bindings = _P3_REPLAY_BINDINGS[run_name]
        ctx = _p3_definition_ctx()
        draft = _p3_draft_yaml(packet, bindings)

        rejection = workflow_update_module._definition_plane_preflight_reject(ctx, draft)

        assert rejection is not None
        assert rejection.unreferenced_parameter_keys == tuple(sorted(bindings))
        error = workflow_update_module._definition_plane_reject_error(rejection)
        assert packet["reject_summaries"]
        for summary in packet["reject_summaries"]:
            assert ("Failed: " + error).startswith(_recorded_summary_prefix(summary["recorded_summary"]))
        for stored in packet["stored_rejects"]:
            assert stored["reason_code"] == "definition_contract_unsatisfied"
            assert stored["verdict"] == "authoring_rejected"

    def test_run1_recorded_reject_error_names_exactly_the_recorded_halt_keys(self) -> None:
        packet = _P3_PACKETS_BY_RUN["run1"]
        recorded_keys = packet["turn_halts"][0]["unresolved_parameter_keys"]
        assert sorted(_P3_REPLAY_BINDINGS["run1"]) == recorded_keys
        ctx = _p3_definition_ctx()
        draft = _p3_draft_yaml(packet, recorded_keys)

        rejection = workflow_update_module._definition_plane_preflight_reject(ctx, draft)

        assert rejection is not None
        error = workflow_update_module._definition_plane_reject_error(rejection)
        # The contract is that the error names every unreferenced key and nothing else; the
        # surrounding prose is free to change.
        assert sorted(re.findall(r"`([^`]+)`", error)) == recorded_keys


class TestScoutedSpineRepeatedOmissionSeam:
    @staticmethod
    def _uncovered_record(selector: str) -> dict[str, object]:
        return {"tool_name": "click", "method": "click", "selector": selector, "trajectory_index": 1}

    def test_repeated_flag_reopens_imposition_for_attempt_three(self) -> None:
        ctx = _code_only_ctx()
        assert workflow_update_module._should_impose_after_update_attempt(ctx) is False
        assert workflow_update_module._should_impose_after_update_attempt(ctx, repeated_identical_omission=True) is True

    @staticmethod
    def _omitting_draft() -> str:
        return _freehand_block_yaml('print(await page.locator("body").inner_text())')

    def test_returning_omission_after_differing_draft_does_not_misfire_imposition(self) -> None:
        ctx = _code_only_ctx()
        _enable_imposition(ctx)
        ctx.scout_trajectory = [
            {
                "tool_name": "type_text",
                "selector": "#a",
                "source_url": "https://example.com/records",
                "trajectory_index": 0,
            },
            {
                "tool_name": "type_text",
                "selector": "#b",
                "source_url": "https://example.com/records",
                "trajectory_index": 1,
            },
        ]
        omitting = _freehand_block_yaml('await page.goto("https://example.com/records", wait_until="domcontentloaded")')
        differing = _freehand_block_yaml('await page.locator("#a").fill("x")')
        omitting_result = workflow_update_module._pre_persist_scouted_spine_result(omitting, ctx)
        assert omitting_result is not None
        ctx.scouted_spine_repeated_identical_missing_steps = True
        ctx.scouted_spine_previous_omission_digest = omitting_result.omission_digest

        assert workflow_update_module._current_draft_repeats_prior_scouted_spine_omission(differing, ctx) is False
        assert ctx.scouted_spine_repeated_identical_missing_steps is False
        assert workflow_update_module._current_draft_repeats_prior_scouted_spine_omission(omitting, ctx) is False

    def test_identical_resubmission_no_op_still_names_at_persist_seam(self) -> None:
        ctx = _code_only_ctx()
        _enable_imposition(ctx)
        draft = self._omitting_draft()
        ctx.update_workflow_called = True
        ctx.last_workflow_yaml = draft
        ctx.scouted_spine_repeated_identical_missing_steps = True

        imposition = workflow_update_module._maybe_impose_synthesized_code_block(draft, ctx)
        assert ctx.spine_imposition_owned_attempt is False
        assert imposition.violations == []

        naming = workflow_update_module._pre_persist_scouted_spine_result(draft, ctx)
        assert naming is not None
        assert "#search-submit" in naming.violations[0]


def _no_goal_value_metadata(label: str, declared_goal: str) -> dict:
    metadata = _terminal_metadata(label, declared_goal)
    for outcome in metadata["claimed_outcomes"]:
        outcome["goal_value_paths"] = []
    for expectation in metadata["terminal_verifier_expectations"]:
        expectation["goal_value_paths"] = []
    return metadata


class TestScoutCredentialSegments:
    _LOGIN_URL = "https://example.com/login"

    def _credential_trajectory(self) -> list[dict[str, Any]]:
        return [
            {
                "tool_name": "fill_credential_field",
                "selector": "#user",
                "source_url": self._LOGIN_URL,
                "credential_id": "cred_1",
                "credential_field": "username",
            },
            {
                "tool_name": "fill_credential_field",
                "selector": "#pass",
                "source_url": self._LOGIN_URL,
                "credential_id": "cred_1",
                "credential_field": "password",
            },
            {
                "tool_name": "click",
                "selector": "#sign-in",
                "role": "button",
                "accessible_name": "Sign in",
                "source_url": self._LOGIN_URL,
            },
            {
                "tool_name": "click",
                "selector": "#reports-nav",
                "role": "link",
                "accessible_name": "Reports",
                "source_url": "https://example.com/dashboard",
            },
            {
                "tool_name": "read_value",
                "read_expression": "document.querySelector('#total').textContent",
                "read_output_path": "output.total",
                "source_url": "https://example.com/dashboard/reports",
            },
        ]

    def test_the_value_read_runs_outside_the_login_presence_guard(self) -> None:
        # SKY-13226: the guard exists so an authenticated replay skips the login, but it indented
        # every later step, so the read never ran and the block returned a name it never bound
        # (UnboundLocalError: _read_value_0, live runs M1/N1/Y1).
        url = "https://example.com/web"
        trajectory = [
            {
                "tool_name": "fill_credential_field",
                "selector": "#user",
                "source_url": url,
                "credential_id": "cred_1",
                "credential_field": "username",
            },
            {
                "tool_name": "fill_credential_field",
                "selector": "#pass",
                "source_url": url,
                "credential_id": "cred_1",
                "credential_field": "password",
            },
            {
                "tool_name": "click",
                "selector": "#sign-in",
                "role": "button",
                "accessible_name": "Sign in",
                "source_url": url,
            },
            {
                "tool_name": "read_value",
                "read_expression": "document.querySelector('#total').textContent",
                "read_output_path": "output.total",
                "source_url": url,
            },
        ]

        synthesized = workflow_update_module.synthesize_code_block(trajectory, strict_selectors=True)

        assert synthesized is not None
        lines = synthesized.code.splitlines()
        guard = next(index for index, line in enumerate(lines) if ".count() == 1:" in line)
        read = next(index for index, line in enumerate(lines) if "_read_value_0 = await" in line)
        guard_indent = len(lines[guard]) - len(lines[guard].lstrip())

        assert read > guard
        assert (len(lines[read]) - len(lines[read].lstrip())) <= guard_indent
        ast.parse(textwrap.dedent(synthesized.code).strip())

    def test_segments_are_valid_self_contained_python(self) -> None:
        synthesized = workflow_update_module.synthesize_code_block(self._credential_trajectory(), strict_selectors=True)
        assert synthesized is not None
        assert len(synthesized.segments) == 3

        for segment in synthesized.segments:
            ast.parse(textwrap.dedent(segment.code).strip())

    def test_credential_reaches_only_the_login_segment(self) -> None:
        synthesized = workflow_update_module.synthesize_code_block(self._credential_trajectory(), strict_selectors=True)
        assert synthesized is not None

        keys_per_segment = [[str(param.get("key")) for param in segment.parameters] for segment in synthesized.segments]

        assert any(keys for keys in keys_per_segment[:1])
        assert all(not keys for keys in keys_per_segment[1:])
        assert ".password" not in "".join(segment.code for segment in synthesized.segments[1:])

    def test_each_scouted_action_appears_in_exactly_one_segment(self) -> None:
        synthesized = workflow_update_module.synthesize_code_block(self._credential_trajectory(), strict_selectors=True)
        assert synthesized is not None

        for selector in ("#pass", "#sign-in", "#reports-nav"):
            hits = sum(1 for segment in synthesized.segments if f'.locator("{selector}").' in segment.code)
            assert hits == 1, f"{selector} appeared in {hits} segments"

    def test_value_read_lands_in_the_last_segment(self) -> None:
        synthesized = workflow_update_module.synthesize_code_block(self._credential_trajectory(), strict_selectors=True)
        assert synthesized is not None

        assert "page.evaluate" in synthesized.segments[-1].code
        assert not any("page.evaluate" in segment.code for segment in synthesized.segments[:-1])

    def test_trajectory_without_credential_fill_is_not_segmented(self) -> None:
        trajectory = [
            {"tool_name": "click", "selector": "#a", "source_url": self._LOGIN_URL},
            {"tool_name": "click", "selector": "#b", "source_url": self._LOGIN_URL},
        ]

        synthesized = workflow_update_module.synthesize_code_block(trajectory, strict_selectors=True)

        assert synthesized is not None
        assert synthesized.segments == []

    def test_no_identifiable_login_submit_is_not_segmented(self) -> None:
        trajectory = [
            {
                "tool_name": "fill_credential_field",
                "selector": "#user",
                "source_url": self._LOGIN_URL,
                "credential_id": "cred_1",
                "credential_field": "username",
            },
            {
                "tool_name": "read_value",
                "read_expression": "document.querySelector('#total').textContent",
                "read_output_path": "output.total",
                "source_url": "https://other.example.com/dashboard",
            },
        ]

        synthesized = workflow_update_module.synthesize_code_block(trajectory, strict_selectors=True)

        assert synthesized is not None
        assert synthesized.segments == []

    def test_login_only_trajectory_is_not_segmented(self) -> None:
        synthesized = workflow_update_module.synthesize_code_block(
            self._credential_trajectory()[:3], strict_selectors=True
        )

        assert synthesized is not None
        assert synthesized.segments == []

    def test_superseded_reads_do_not_shift_the_boundary(self) -> None:
        trajectory: list[dict[str, Any]] = [
            {
                "tool_name": "read_value",
                "read_expression": "early",
                "read_output_path": "output.total",
                "source_url": self._LOGIN_URL,
            },
            *self._credential_trajectory(),
        ]

        synthesized = workflow_update_module.synthesize_code_block(trajectory, strict_selectors=True)

        assert synthesized is not None
        assert len(synthesized.segments) == 3
        assert "credential" in "".join(str(p.get("key")) for p in synthesized.segments[0].parameters)
        assert "page.evaluate" in synthesized.segments[-1].code

    def test_resolver_prefers_segments_over_the_per_interaction_derivation(self) -> None:
        synthesized = workflow_update_module.synthesize_code_block(self._credential_trajectory(), strict_selectors=True)
        assert synthesized is not None

        resolved = workflow_update_module._resolve_durable_stages(synthesized, source_code=synthesized.code)

        assert resolved.segmented is True
        assert len(resolved.codes) == 3
        for code in resolved.codes:
            ast.parse(code)

    def test_split_places_segments_and_scopes_keys(self) -> None:
        code_block: dict[str, Any] = {"block_type": "code", "label": "get_total", "code": "placeholder"}
        parsed: dict[str, Any] = {"workflow_definition": {"blocks": [code_block]}}
        stage_codes = [
            'await page.locator("#user").fill(str(cred.username))',
            'await page.locator("#reports-nav").click()',
        ]

        with capture_logs() as logs:
            violations = workflow_update_module._split_selected_output_owner_into_browser_stages(
                parsed=parsed,
                code_block=code_block,
                stage_codes=stage_codes,
                extraction_code='value = await page.locator("#t").inner_text()\nreturn {"output": {"total": value}}',
                parameter_keys=["cred"],
                segmented=True,
            )

        assert violations == []
        blocks = parsed["workflow_definition"]["blocks"]
        assert [block.get("label") for block in blocks] == [
            "get_total_browser_stage_1",
            "get_total_browser_stage_2",
            "get_total",
        ]
        assert blocks[0].get("parameter_keys") == ["cred"]
        assert "parameter_keys" not in blocks[1]
        emitted = [entry for entry in logs if entry.get("event") == "copilot_output_owner_split_into_browser_stages"]
        assert len(emitted) == 1
        assert emitted[0]["credential_grouped"] is True

    def test_split_declines_when_another_block_routes_to_the_output_owner(self) -> None:
        code_block: dict[str, Any] = {"block_type": "code", "label": "get_total", "code": "placeholder"}
        upstream = {"block_type": "code", "label": "prep", "code": "pass", "next_block_label": "get_total"}
        parsed: dict[str, Any] = {"workflow_definition": {"blocks": [upstream, code_block]}}

        violations = workflow_update_module._split_selected_output_owner_into_browser_stages(
            parsed=parsed,
            code_block=code_block,
            stage_codes=["await page.locator('#a').click()", "await page.locator('#b').click()"],
            extraction_code='return {"output": {"total": 1}}',
            parameter_keys=[],
            segmented=True,
        )

        assert violations
        assert [block.get("label") for block in parsed["workflow_definition"]["blocks"]] == ["prep", "get_total"]

    def test_synthesized_step_action_types_use_the_shared_action_vocabulary(self) -> None:
        from skyvern.forge.sdk.workflow.models.block import CodeBlockStep

        synthesized = workflow_update_module.synthesize_code_block(self._credential_trajectory(), strict_selectors=True)
        assert synthesized is not None
        assert any(str(step.get("description", "")).startswith("Fill ") for step in synthesized.steps)

        for step in synthesized.steps:
            CodeBlockStep(**step)

    @pytest.mark.asyncio
    async def test_fused_credential_draft_persists_as_separated_blocks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_successful_update(monkeypatch)

        async def _no_credential_validation_error(_value: object, _ctx: object) -> None:
            return None

        monkeypatch.setattr(
            workflow_update_module, "_credential_reference_validation_error", _no_credential_validation_error
        )
        ctx = _code_only_ctx()
        _enable_imposition(ctx)
        ctx.scout_trajectory = self._credential_trajectory()[:4]
        ctx.scouted_credential_field_inventory_by_credential_id = {"cred_1": frozenset({"username", "password"})}
        synthesized = workflow_update_module.synthesize_code_block(ctx.scout_trajectory, strict_selectors=True)
        assert synthesized is not None
        submitted_code = (
            synthesized.code.rstrip()
            + '\nvalue = await page.locator("#total").inner_text()\nreturn {"output": {"total": value}}\n'
        )
        submitted = yaml.safe_dump(
            {
                "title": "Dashboard total",
                "workflow_definition": {
                    "parameters": [],
                    "blocks": [{"block_type": "code", "label": "get_total", "code": submitted_code}],
                },
            },
            sort_keys=False,
        )
        metadata = [_no_goal_value_metadata("get_total", "read the dashboard total")]

        result = await _update_workflow({"workflow_yaml": submitted, "code_artifact_metadata": metadata}, ctx)

        assert result["ok"] is True
        parsed = parse_workflow_yaml(ctx.workflow_yaml)
        assert isinstance(parsed, dict)
        code_blocks = [block for block in workflow_blocks(parsed) if block.get("block_type") == "code"]
        assert [str(block.get("label") or "") for block in code_blocks][-1] == "get_total"
        assert len(code_blocks) >= 3
        stage_codes = [str(block.get("code") or "") for block in code_blocks[:-1]]
        owner_code = str(code_blocks[-1].get("code") or "")
        login_positions = [index for index, code in enumerate(stage_codes) if ".password" in code]
        nav_positions = [
            index for index, code in enumerate(stage_codes) if 'page.locator("#reports-nav").click()' in code
        ]
        assert login_positions and nav_positions
        assert max(login_positions) < min(nav_positions)
        assert ".password" not in owner_code
        assert "inner_text" in owner_code
        for code in (*stage_codes, owner_code):
            ast.parse(textwrap.dedent(code).strip())

    @pytest.mark.asyncio
    async def test_fused_draft_without_extraction_boundary_names_the_missing_boundary(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)

        async def _no_credential_validation_error(_value: object, _ctx: object) -> None:
            return None

        monkeypatch.setattr(
            workflow_update_module, "_credential_reference_validation_error", _no_credential_validation_error
        )
        ctx = _code_only_ctx()
        _enable_imposition(ctx)
        ctx.scout_trajectory = self._credential_trajectory()[:4]
        ctx.scouted_credential_field_inventory_by_credential_id = {"cred_1": frozenset({"username", "password"})}
        synthesized = workflow_update_module.synthesize_code_block(ctx.scout_trajectory, strict_selectors=True)
        assert synthesized is not None
        # Same draft as the split case minus the extraction suffix: stages derive, nothing to split at.
        submitted = yaml.safe_dump(
            {
                "title": "Dashboard total",
                "workflow_definition": {
                    "parameters": [],
                    "blocks": [{"block_type": "code", "label": "get_total", "code": synthesized.code}],
                },
            },
            sort_keys=False,
        )
        metadata = [_no_goal_value_metadata("get_total", "read the dashboard total")]

        with capture_logs() as logs:
            result = await _update_workflow({"workflow_yaml": submitted, "code_artifact_metadata": metadata}, ctx)

        assert result["ok"] is True
        emitted = [
            entry for entry in logs if entry.get("event") == "copilot_durable_stage_split_without_extraction_boundary"
        ]
        assert len(emitted) == 1
        assert emitted[0]["stage_count"] >= 2
        assert emitted[0]["block_label"] == "get_total"
        parsed = parse_workflow_yaml(ctx.workflow_yaml)
        assert isinstance(parsed, dict)
        labels = [str(block.get("label") or "") for block in workflow_blocks(parsed)]
        assert not any("browser_stage" in label for label in labels)


def test_a_goal_path_is_rooted_where_the_plan_returns_the_requested_output_namespace() -> None:
    # Live shape (SKY-13226): the plan's extraction returned {"output": {"visitors": ...}} while the
    # block metadata named the same value "visitors", so imposition refused code that returned it.
    rooted = workflow_update_module._goal_paths_rooted_as_the_code_returns_them(
        {"visitors"}, 'return {"output": {"visitors": _extraction_value_0}}'
    )

    assert rooted == {"output.visitors"}


def test_a_goal_path_stays_bare_when_the_block_returns_a_bare_mapping() -> None:
    bare = workflow_update_module._goal_paths_rooted_as_the_code_returns_them(
        {"records"}, 'return {"records": records}'
    )

    assert bare == {"records"}
