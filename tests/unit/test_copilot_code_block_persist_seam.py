"""Tests for the code-block persist seam in `_update_workflow`.

OSS-synced: only example.* / RFC-2606 placeholder targets and synthetic labels.
"""

from __future__ import annotations

import ast
import json
import textwrap
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml
from structlog.testing import capture_logs

from skyvern.forge.sdk.copilot import agent as agent_module
from skyvern.forge.sdk.copilot import enforcement as enforcement_module
from skyvern.forge.sdk.copilot.blocker_signal import (
    CREDENTIAL_SCOUT_VERIFY_REPLY,
    CopilotToolBlockerSignal,
    assert_clean_user_facing_text,
)
from skyvern.forge.sdk.copilot.build_test_outcome import (
    BuildTestOutcomeReasonCode,
    RecordedBuildTestOutcome,
    RecordedOutcomeBindingConstraint,
    authored_block_signatures_from_workflow,
    authored_structure_signature_from_workflow,
    latest_recorded_build_test_outcome_repeated,
    record_build_test_outcome,
    recorded_outcome_from_author_time_reject,
    recorded_outcome_from_authoring_repair_context,
    recorded_outcome_from_run_blocks_result,
)
from skyvern.forge.sdk.copilot.code_block_preflight import (
    SANDBOX_UNRESOLVED_NAME_REASON_CODE,
    _sandbox_shim_surface,
    strip_redundant_sandbox_imports,
)
from skyvern.forge.sdk.copilot.code_block_security import CodeBlockSecurityError
from skyvern.forge.sdk.copilot.code_block_synthesis import (
    SynthesisDiagnostics,
    SynthesizedCodeBlock,
    _get_by_role_expr,
    _get_by_role_expr_strict,
)
from skyvern.forge.sdk.copilot.completion_verification import CompletionVerificationResult, CriterionVerdict
from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy
from skyvern.forge.sdk.copilot.context import CodeAuthoringRepairContext, CopilotContext, FillCarry
from skyvern.forge.sdk.copilot.enforcement import (
    MAX_CODE_AUTHORING_GUARDRAIL_REJECTS,
    MAX_CREDENTIAL_PRIORITY_AUTHORING_REJECTS,
    _check_enforcement,
)
from skyvern.forge.sdk.copilot.output_contracts import (
    OutputContractAdvisoryState,
    code_block_available_binding_keys_by_label,
)
from skyvern.forge.sdk.copilot.output_utils import sanitize_tool_result_for_llm
from skyvern.forge.sdk.copilot.reached_download_target import ReachedDownloadTarget
from skyvern.forge.sdk.copilot.request_policy import CompletionCriterion, JudgmentTruthCondition, RequestPolicy
from skyvern.forge.sdk.copilot.run_outcome import TERMINAL_CHALLENGE_BLOCKER_REASON_CODE, RecordedRunOutcome
from skyvern.forge.sdk.copilot.runtime import AgentContext
from skyvern.forge.sdk.copilot.tools import (
    _code_block_safety_errors,
    _detect_stale_block_metadata,
    _update_workflow,
)
from skyvern.forge.sdk.copilot.tools import scouting as scouting_module
from skyvern.forge.sdk.copilot.tools import workflow_update as workflow_update_module
from skyvern.forge.sdk.copilot.tools.workflow_update import (
    _code_safety_reject_payload,
    _OutputContractEvaluation,
    _strip_redundant_sandbox_imports_in_yaml,
)
from skyvern.forge.sdk.copilot.turn_halt import (
    CopilotTurnHalt,
    TurnHalt,
    TurnHaltKind,
    TurnHaltVerdict,
    _kind_for_blocker_signal,
    stash_repair_ceiling_turn_halt,
)
from skyvern.forge.sdk.copilot.workflow_credential_utils import parse_workflow_yaml, workflow_blocks
from skyvern.forge.sdk.workflow.exceptions import InsecureCodeDetected
from skyvern.forge.sdk.workflow.models.block import CodeBlock


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

_RAW_SECRET_OUTPUT_POLICY_YAML = _yaml(
    """
    title: Registry lookup
    workflow_definition:
      blocks:
      - block_type: navigation
        label: login
        navigation_goal: Type password: hunter2 into the password field.
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
    monkeypatch.setattr(workflow_update_module, "composition_page_evidence_error", lambda *_args, **_kwargs: None)


def _single_code_block(parsed: dict[str, object]) -> dict[str, object]:
    blocks = [block for block in workflow_blocks(parsed) if str(block.get("block_type") or "").lower() == "code"]
    assert len(blocks) == 1
    return blocks[0]


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

    @pytest.mark.asyncio
    async def test_denied_page_api_attribute_is_repairable_preflight_error_without_duplicate_generic_message(
        self,
    ) -> None:
        result = await _update_workflow(
            {"workflow_yaml": _code_yaml("text = await page.evaluate('() => document.body.innerText')")},
            _standard_ctx(),
        )

        assert result["ok"] is False
        joined = result["error"]
        assert "AUTHOR_PAGE_EVALUATE" in joined
        assert "failed the generated-code preflight check" in joined
        assert joined.count("failed the sandbox safety check") == 0
        assert joined.count("page.evaluate is not allowed") == 1

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
        for expected in (
            "value",
            "count",
            "x",
            "branch_value",
            "NotRunnable",
        ):
            assert expected in joined
        assert "unresolved names: `branch_value`, `count`, `page`, `value`, `x`" in joined

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
        assert "recovered" in errors[0]
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
        assert "authoring_repair_context" not in result["data"]
        assert ctx.last_code_authoring_repair_context is None

    @pytest.mark.parametrize(
        "workflow_yaml",
        [
            _code_yaml("import os\nprint(provider_query)"),
            _code_yaml("await page.evaluate(provider_query)"),
        ],
    )
    @pytest.mark.asyncio
    async def test_mixed_primary_authoring_rejects_do_not_carry_repair_context(self, workflow_yaml: str) -> None:
        ctx = _code_only_ctx()

        result = await _update_workflow({"workflow_yaml": workflow_yaml}, ctx)

        assert result["ok"] is False
        assert "authoring_repair_context" not in result["data"]
        assert ctx.last_code_authoring_repair_context is None

    @pytest.mark.asyncio
    async def test_imposition_reject_carries_ambiguous_selector_repair_context(self) -> None:
        ctx = _resale_ctx(refiner_selector="button:nth-of-type(2)")
        private_url = "https://example.com/orders?session=secret-token#account"
        for interaction in ctx.scout_trajectory:
            interaction["source_url"] = private_url

        result = await _update_workflow({"workflow_yaml": _resale_submitted_yaml("button:nth-of-type(2)")}, ctx)

        assert result["ok"] is False
        repair_context = result["data"]["authoring_repair_context"]
        assert repair_context["reason_code"] == "ambiguous_bare_selector"
        assert repair_context["block_label"] == "order_status"
        assert repair_context["selector"] == "button"
        assert repair_context["source_url"] == "https://example.com"
        assert "secret-token" not in str(repair_context)
        assert repair_context["refiner_selector"] is None
        assert repair_context["selector_alternatives"] == [
            {"tool_name": "type_text", "role": "textbox", "selector": "#order-id"}
        ]
        assert isinstance(ctx.last_code_authoring_repair_context, CodeAuthoringRepairContext)
        assert ctx.last_code_authoring_repair_context.model_dump(mode="json") == repair_context

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

    @pytest.mark.asyncio
    async def test_imposition_reject_replaces_stale_repair_context(self) -> None:
        ctx = _resale_ctx(refiner_selector="button:nth-of-type(2)")
        ctx.last_code_authoring_repair_context = _stale_unresolved_repair_context()

        result = await _update_workflow({"workflow_yaml": _resale_submitted_yaml("button:nth-of-type(2)")}, ctx)

        assert result["ok"] is False
        assert result["data"]["authoring_repair_context"]["reason_code"] == "ambiguous_bare_selector"
        assert result["data"]["authoring_repair_context"]["selector_alternatives"] == [
            {"tool_name": "type_text", "role": "textbox", "selector": "#order-id"}
        ]
        assert ctx.last_code_authoring_repair_context != _stale_unresolved_repair_context()


class TestCodeRepairProgressClassification:
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
                "AUTHOR_PAGE_EVALUATE: page.evaluate is not allowed."
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
                "AUTHOR_PAGE_EVALUATE: page.evaluate is not allowed."
            ]
        )

        assert payload is None

    @pytest.mark.asyncio
    async def test_authoritative_recorded_outcome_rejects_unchanged_authored_candidate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        ctx.workflow_yaml = _code_yaml('await page.goto("https://example.com/old")')
        signature = authored_structure_signature_from_workflow(_SAFE_CODE_YAML)
        ctx.latest_recorded_build_test_outcome = RecordedBuildTestOutcome(
            phase="persisted_block_run",
            attempted_tool="update_and_run_blocks",
            verdict="repairable_failure",
            reason_code="outcome_not_demonstrated",
            structural_failure_identity="completion:typed-outcome",
            authored_structure_signature=signature,
        )

        result = await _update_workflow({"workflow_yaml": _SAFE_CODE_YAML}, ctx, allow_missing_credentials=True)

        assert result["ok"] is False
        assert "left the frontier the last recorded test outcome named unchanged" in result["error"]
        assert ctx.workflow_yaml == _code_yaml('await page.goto("https://example.com/old")')
        assert ctx.has_staged_proposal is False
        assert ctx.latest_recorded_build_test_outcome is not None
        assert ctx.latest_recorded_build_test_outcome.reason_code == "unchanged_after_recorded_outcome"
        assert ctx.latest_recorded_build_test_outcome.structural_key is not None

    @pytest.mark.asyncio
    async def test_repeated_unchanged_authored_candidate_keeps_same_reject_key_and_backstop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        signature = authored_structure_signature_from_workflow(_SAFE_CODE_YAML)
        ctx.latest_recorded_build_test_outcome = RecordedBuildTestOutcome(
            phase="persisted_block_run",
            attempted_tool="update_and_run_blocks",
            verdict="repairable_failure",
            reason_code="outcome_not_demonstrated",
            structural_failure_identity="completion:typed-outcome",
            authored_structure_signature=signature,
        )

        first = await _update_workflow({"workflow_yaml": _SAFE_CODE_YAML}, ctx, allow_missing_credentials=True)
        first_key = ctx.latest_recorded_build_test_outcome.structural_key
        first_count = ctx.code_authoring_guardrail_reject_count
        second = await _update_workflow({"workflow_yaml": _SAFE_CODE_YAML}, ctx, allow_missing_credentials=True)

        assert first["ok"] is False
        assert second["ok"] is False
        assert first_key is not None
        assert ctx.latest_recorded_build_test_outcome.structural_key == first_key
        assert first_count == 1
        assert ctx.code_authoring_guardrail_reject_count == 2

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
    async def test_authoritative_outcome_not_demonstrated_rejects_output_empty_changed_candidate(
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
        partial_yaml = _yaml(
            f"""
            title: Provider lookup
            workflow_definition:
              parameters:
              - parameter_type: workflow
                workflow_parameter_type: string
                key: address_city_county_or_zip_code
              blocks:
              - block_type: code
                label: {label}
                parameter_keys:
                - address_city_county_or_zip_code
                code: |
                  await page.locator("#locInput").wait_for(state="visible", timeout=15000)
                  await page.locator("#locInput").fill(str(address_city_county_or_zip_code))
                  return {{}}
            """
        )

        result = await _update_workflow({"workflow_yaml": partial_yaml}, ctx, allow_missing_credentials=True)

        assert result["ok"] is False
        assert "does not return any keyed output" in result["error"]
        assert ctx.has_staged_proposal is False
        assert ctx.latest_recorded_build_test_outcome is not None
        assert ctx.latest_recorded_build_test_outcome.reason_code == "metadata_reject"
        assert ctx.latest_recorded_build_test_outcome.is_authoritative is True

    @pytest.mark.asyncio
    async def test_authoritative_outcome_not_demonstrated_rejects_explicit_empty_return_despite_assigned_goal_root(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        label = "lookup_provider_and_extract_credentials"
        ctx.completion_verification_result = CompletionVerificationResult(
            status="evaluated",
            criterion_ids=["npi"],
            verdicts=[
                CriterionVerdict(criterion_id="npi", state="unsatisfied", reason_code="no_evidence"),
            ],
        )
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
        )
        candidate_yaml = _yaml(
            f"""
            title: Provider lookup
            workflow_definition:
              blocks:
              - block_type: code
                label: {label}
                code: |
                  npi = await page.locator("#npi").inner_text(timeout=5000)
                  return {{}}
            """
        )

        result = await _update_workflow({"workflow_yaml": candidate_yaml}, ctx, allow_missing_credentials=True)

        assert result["ok"] is False
        assert "does not return any keyed output" in result["error"]
        assert ctx.has_staged_proposal is False
        outcome = ctx.latest_recorded_build_test_outcome
        assert outcome is not None
        assert outcome.reason_code == "metadata_reject"
        assert outcome.is_authoritative is True
        assert outcome.block_labels == [label]
        assert outcome.structural_failure_identity.startswith("author_time:")

    @pytest.mark.asyncio
    async def test_authoritative_outcome_not_demonstrated_rejects_known_empty_helper_without_missing_facts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        label = "lookup_provider_and_extract_credentials"
        ctx.completion_verification_result = CompletionVerificationResult(
            status="evaluated",
            criterion_ids=["npi"],
            verdicts=[
                CriterionVerdict(criterion_id="npi", state="unsatisfied", reason_code="no_evidence"),
            ],
        )
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
                      return {{}}

                  return build()
            """
        )

        result = await _update_workflow({"workflow_yaml": candidate_yaml}, ctx, allow_missing_credentials=True)

        assert result["ok"] is False
        assert "does not return any keyed output" in result["error"]
        outcome = ctx.latest_recorded_build_test_outcome
        assert outcome is not None
        assert outcome.reason_code == "metadata_reject"
        assert outcome.block_labels == [label]
        assert outcome.missing_requested_output_facts == []
        assert outcome.structural_failure_identity.startswith("author_time:")

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
    async def test_authoritative_outcome_not_demonstrated_rejects_candidate_missing_requested_output_roots(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        label = "lookup_provider_and_extract_credentials"
        ctx.code_artifact_metadata = {
            label: {
                "block_label": label,
                "claimed_outcomes": [{"goal_value_paths": ["address"]}],
                "terminal_verifier_expectations": [{"goal_value_paths": ["address"]}],
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
        partial_yaml = _yaml(
            f"""
            title: Provider lookup
            workflow_definition:
              blocks:
              - block_type: code
                label: {label}
                code: |
                  await page.locator("#locInput").wait_for(state="visible", timeout=15000)
                  address = "North Carolina, USA"
            """
        )

        result = await _update_workflow({"workflow_yaml": partial_yaml}, ctx, allow_missing_credentials=True)

        assert result["ok"] is False
        assert "does not cover the missing requested output paths" in result["error"]
        assert ctx.has_staged_proposal is False
        assert ctx.latest_recorded_build_test_outcome is not None
        assert ctx.latest_recorded_build_test_outcome.reason_code == "metadata_reject"
        assert ctx.latest_recorded_build_test_outcome.is_authoritative is True

    @pytest.mark.asyncio
    async def test_authoritative_outcome_not_demonstrated_accepts_canonical_output_path_candidate_with_sibling(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        label = "submit_form_and_extract_confirmation"
        ctx.code_artifact_metadata = {
            label: {
                "block_label": label,
                "claimed_outcomes": [{"goal_value_paths": ["output.confirmation_number"]}],
                "terminal_verifier_expectations": [{"goal_value_paths": ["output.confirmation_number"]}],
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
        assert "extraction_schema" in ctx.workflow_yaml
        assert ctx.latest_recorded_build_test_outcome is not None
        assert ctx.latest_recorded_build_test_outcome.reason_code == "outcome_not_demonstrated"

    @pytest.mark.asyncio
    async def test_authoritative_outcome_not_demonstrated_rejects_flat_candidate_for_canonical_output_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        label = "submit_form_and_extract_confirmation"
        ctx.code_artifact_metadata = {
            label: {
                "block_label": label,
                "claimed_outcomes": [{"goal_value_paths": ["confirmation_number"]}],
                "terminal_verifier_expectations": [{"goal_value_paths": ["confirmation_number"]}],
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
                  return {{"confirmation_number": "ABC-123"}}
            """
        )

        result = await _update_workflow({"workflow_yaml": candidate_yaml}, ctx, allow_missing_credentials=True)

        assert result["ok"] is False
        assert "output.confirmation_number" in result["error"]
        assert "satisfies the requested output contract" in result["error"]
        assert "canonical required child paths" in result["error"]
        outcome = ctx.latest_recorded_build_test_outcome
        assert outcome is not None
        assert outcome.reason_code == "metadata_reject"
        assert outcome.missing_requested_output_facts == [
            {
                "output_path": "output.confirmation_number",
                "output_root": "output",
                "reason_code": "recorded_outcome_missing_output_coverage",
                "value_status": "no_typed_value",
            }
        ]
        rendered = agent_module._recorded_build_test_outcome_prompt(ctx)
        assert "output_path=output.confirmation_number" in rendered

    def test_required_output_roots_ignore_nested_debug_output_root_without_declared_root(self) -> None:
        label = "extract_provider_profile"
        metadata = {label: {"block_label": label, "claimed_outcomes": [{"goal_value_paths": ["debug_output"]}]}}
        candidate_yaml = _yaml(
            f"""
            title: Provider lookup
            workflow_definition:
              blocks:
              - block_type: code
                label: {label}
                code: |
                  return {{"debug_output": {{"npi": "1234567890"}}}}
            """
        )

        assert workflow_update_module._candidate_missing_required_output_paths(
            candidate_yaml,
            metadata,
            required_paths={"npi"},
        ) == ["npi"]

    def test_required_output_roots_ignore_same_label_undeclared_static_root(self) -> None:
        label = "extract_provider_profile"
        metadata = {label: {"block_label": label, "claimed_outcomes": [{"goal_value_paths": ["address"]}]}}
        candidate_yaml = _yaml(
            f"""
            title: Provider lookup
            workflow_definition:
              blocks:
              - block_type: code
                label: {label}
                code: |
                  return {{"npi": "1234567890"}}
            """
        )

        assert workflow_update_module._candidate_missing_required_output_paths(
            candidate_yaml,
            metadata,
            required_paths={"npi"},
        ) == ["npi"]

    def test_required_output_roots_ignore_static_root_without_label_metadata(self) -> None:
        label = "extract_provider_profile"
        candidate_yaml = _yaml(
            f"""
            title: Provider lookup
            workflow_definition:
              blocks:
              - block_type: code
                label: {label}
                code: |
                  return {{"npi": "1234567890"}}
            """
        )

        assert workflow_update_module._candidate_missing_required_output_paths(
            candidate_yaml,
            {},
            required_paths={"npi"},
        ) == ["npi"]

    def test_required_output_roots_accept_same_label_declared_static_root(self) -> None:
        label = "extract_provider_profile"
        metadata = {label: {"block_label": label, "claimed_outcomes": [{"goal_value_paths": ["npi"]}]}}
        candidate_yaml = _yaml(
            f"""
            title: Provider lookup
            workflow_definition:
              blocks:
              - block_type: code
                label: {label}
                code: |
                  return {{"npi": "1234567890"}}
            """
        )

        assert (
            workflow_update_module._candidate_missing_required_output_paths(
                candidate_yaml,
                metadata,
                required_paths={"npi"},
            )
            == []
        )

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

    def test_local_dict_dynamic_key_preserves_literal_roots_while_abstaining(self) -> None:
        produced = workflow_update_module._code_block_produced_output_roots(
            """
            key = "npi"
            result = {"address": "North Carolina", key: "123"}
            return result
            """
        )

        assert produced.roots == {"address"}
        assert produced.abstained is True

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
    async def test_authoritative_outcome_not_demonstrated_rejects_unrelated_dynamic_output_shape(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        label = "lookup_provider_and_extract_credentials"
        ctx.code_artifact_metadata = {
            label: {
                "block_label": label,
                "claimed_outcomes": [{"goal_value_paths": ["address"]}],
                "terminal_verifier_expectations": [{"goal_value_paths": ["address"]}],
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
                  key = "address"
                  result[key] = await page.locator("#address").inner_text(timeout=5000)
                  return result
            """
        )

        result = await _update_workflow({"workflow_yaml": candidate_yaml}, ctx, allow_missing_credentials=True)

        assert result["ok"] is False
        assert "does not cover the missing requested output paths" in result["error"]
        outcome = ctx.latest_recorded_build_test_outcome
        assert outcome is not None
        assert outcome.reason_code == "metadata_reject"
        assert outcome.missing_requested_output_facts == [
            {
                "output_path": "npi",
                "output_root": "npi",
                "reason_code": "recorded_outcome_missing_output_coverage",
                "value_status": "no_typed_value",
            }
        ]

    @pytest.mark.asyncio
    async def test_authoritative_outcome_not_demonstrated_rejects_when_unrelated_block_abstains(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        declared_label = "extract_provider_profile"
        unrelated_label = "extract_unrelated_status"
        ctx.code_artifact_metadata = {
            declared_label: {
                "block_label": declared_label,
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
                label: {declared_label}
                code: |
                  return {{"address": "North Carolina"}}
              - block_type: code
                label: {unrelated_label}
                code: |
                  result = {{}}
                  key = "status"
                  result[key] = await page.locator("#status").inner_text(timeout=5000)
                  return result
            """
        )

        result = await _update_workflow({"workflow_yaml": candidate_yaml}, ctx, allow_missing_credentials=True)

        assert result["ok"] is False
        assert "does not cover the missing requested output paths" in result["error"]
        outcome = ctx.latest_recorded_build_test_outcome
        assert outcome is not None
        assert outcome.reason_code == "metadata_reject"
        assert outcome.missing_requested_output_facts == [
            {
                "output_path": "npi",
                "output_root": "npi",
                "reason_code": "recorded_outcome_missing_output_coverage",
                "value_status": "no_typed_value",
            }
        ]

    @pytest.mark.asyncio
    async def test_authoritative_outcome_not_demonstrated_keeps_undeclared_missing_roots_with_dynamic_shape(
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
                {"output_path": "status", "output_root": "status", "value_status": "no_typed_value"},
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
                  result[key] = await page.locator("#npi").inner_text(timeout=5000)
                  return result
            """
        )

        result = await _update_workflow({"workflow_yaml": candidate_yaml}, ctx, allow_missing_credentials=True)

        assert result["ok"] is False
        assert "does not cover the missing requested output paths" in result["error"]
        outcome = ctx.latest_recorded_build_test_outcome
        assert outcome is not None
        assert outcome.reason_code == "metadata_reject"
        assert outcome.missing_requested_output_facts == [
            {
                "output_path": "status",
                "output_root": "status",
                "reason_code": "recorded_outcome_missing_output_coverage",
                "value_status": "no_typed_value",
            }
        ]

    @pytest.mark.asyncio
    async def test_authoritative_outcome_not_demonstrated_rejects_literal_output_missing_required_root(
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
                  return {{"address": "North Carolina"}}
            """
        )

        result = await _update_workflow({"workflow_yaml": candidate_yaml}, ctx, allow_missing_credentials=True)

        assert result["ok"] is False
        assert "does not cover the missing requested output paths" in result["error"]
        outcome = ctx.latest_recorded_build_test_outcome
        assert outcome is not None
        assert outcome.reason_code == "metadata_reject"
        assert outcome.block_labels == [label]
        assert outcome.missing_requested_output_facts == [
            {
                "output_path": "npi",
                "output_root": "npi",
                "reason_code": "recorded_outcome_missing_output_coverage",
                "value_status": "no_typed_value",
            }
        ]

    @pytest.mark.asyncio
    async def test_authoritative_outcome_not_demonstrated_rejects_empty_return_despite_assigned_root(
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
                  npi = await page.locator("#npi").inner_text(timeout=5000)
                  return {{}}
            """
        )

        result = await _update_workflow({"workflow_yaml": candidate_yaml}, ctx, allow_missing_credentials=True)

        assert result["ok"] is False
        assert "does not return any keyed output" in result["error"]
        outcome = ctx.latest_recorded_build_test_outcome
        assert outcome is not None
        assert outcome.reason_code == "metadata_reject"
        assert outcome.block_labels == [label]
        assert outcome.missing_requested_output_facts == []

    @pytest.mark.parametrize("return_expression", ["None", "[]", '["not keyed"]'])
    @pytest.mark.asyncio
    async def test_authoritative_outcome_not_demonstrated_rejects_static_non_keyed_return_with_unrelated_output(
        self, monkeypatch: pytest.MonkeyPatch, return_expression: str
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        declared_label = "extract_provider_profile"
        unrelated_label = "extract_unrelated_status"
        ctx.code_artifact_metadata = {
            declared_label: {
                "block_label": declared_label,
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
                label: {declared_label}
                code: |
                  return {return_expression}
              - block_type: code
                label: {unrelated_label}
                code: |
                  return {{"status": "active"}}
            """
        )

        result = await _update_workflow({"workflow_yaml": candidate_yaml}, ctx, allow_missing_credentials=True)

        assert result["ok"] is False
        assert "does not cover the missing requested output paths" in result["error"]
        outcome = ctx.latest_recorded_build_test_outcome
        assert outcome is not None
        assert outcome.reason_code == "metadata_reject"
        assert outcome.missing_requested_output_facts == [
            {
                "output_path": "npi",
                "output_root": "npi",
                "reason_code": "recorded_outcome_missing_output_coverage",
                "value_status": "no_typed_value",
            }
        ]

    @pytest.mark.asyncio
    async def test_authoritative_outcome_not_demonstrated_rejects_helper_known_empty_output(
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
                      return {{}}

                  return build()
            """
        )

        result = await _update_workflow({"workflow_yaml": candidate_yaml}, ctx, allow_missing_credentials=True)

        assert result["ok"] is False
        assert "does not return any keyed output" in result["error"]
        outcome = ctx.latest_recorded_build_test_outcome
        assert outcome is not None
        assert outcome.reason_code == "metadata_reject"
        assert outcome.missing_requested_output_facts == []

    @pytest.mark.asyncio
    async def test_metadata_reject_persists_missing_roots_for_next_author_context(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        label = "lookup_provider_and_extract_credentials"
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
        partial_yaml = _yaml(
            f"""
            title: Provider lookup
            workflow_definition:
              blocks:
              - block_type: code
                label: {label}
                code: |
                  await page.locator("#locInput").wait_for(state="visible", timeout=15000)
                  return {{"search_options": []}}
            """
        )

        result = await _update_workflow({"workflow_yaml": partial_yaml}, ctx, allow_missing_credentials=True)

        assert result["ok"] is False
        outcome = ctx.latest_recorded_build_test_outcome
        assert outcome is not None
        assert outcome.reason_code == "metadata_reject"
        assert outcome.missing_requested_output_facts == [
            {
                "output_path": "address",
                "output_root": "address",
                "reason_code": "recorded_outcome_missing_output_coverage",
                "value_status": "no_typed_value",
            },
            {
                "output_path": "credentialing_status",
                "output_root": "credentialing_status",
                "reason_code": "recorded_outcome_missing_output_coverage",
                "value_status": "no_typed_value",
            },
        ]
        rendered = agent_module._recorded_build_test_outcome_prompt(ctx)
        assert "missing_requested_output_facts:" in rendered
        assert "output_root=address" in rendered
        assert "output_root=credentialing_status" in rendered
        assert "value_status=no_typed_value" in rendered

    @pytest.mark.asyncio
    async def test_authoritative_outcome_not_demonstrated_rejects_broad_output_for_missing_child_paths(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        label = "lookup_entry"
        ctx.code_artifact_metadata = {
            label: {
                "block_label": label,
                "claimed_outcomes": [{"goal_value_paths": ["output"]}],
                "terminal_verifier_expectations": [{"goal_value_paths": ["output"]}],
            }
        }
        ctx.workflow_verification_evidence.code_artifact_metadata = dict(ctx.code_artifact_metadata)
        ctx.verified_block_outputs[label] = {
            "output": {"npi": "1234567890", "address": "Example location", "statuses": ["active"]}
        }
        ctx.latest_recorded_build_test_outcome = RecordedBuildTestOutcome(
            phase="persisted_block_run",
            attempted_tool="update_and_run_blocks",
            verdict="repairable_failure",
            reason_code="outcome_not_demonstrated",
            structural_failure_identity="completion:unsatisfied-output",
            authored_structure_signature="authored:previous-failed-candidate",
            missing_requested_output_facts=[
                {"output_path": "output.npi", "output_root": "output", "value_status": "no_typed_value"},
                {"output_path": "output.address", "output_root": "output", "value_status": "no_typed_value"},
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
                  return {{"output": {{"summary": "found"}}}}
            """
        )

        result = await _update_workflow({"workflow_yaml": candidate_yaml}, ctx, allow_missing_credentials=True)

        assert result["ok"] is False
        assert "requested output contract" in result["error"]
        assert result["data"]["canonical_required_child_paths"] == [
            "output.address",
            "output.npi",
            "output.statuses",
        ]
        outcome = ctx.latest_recorded_build_test_outcome
        assert outcome is not None
        assert outcome.reason_code == "metadata_reject"
        assert outcome.missing_requested_output_facts == [
            {
                "output_path": "output.address",
                "output_root": "output",
                "reason_code": "recorded_outcome_missing_output_coverage",
                "value_status": "no_typed_value",
            },
            {
                "output_path": "output.npi",
                "output_root": "output",
                "reason_code": "recorded_outcome_missing_output_coverage",
                "value_status": "no_typed_value",
            },
            {
                "output_path": "output.statuses",
                "output_root": "output",
                "reason_code": "recorded_outcome_missing_output_coverage",
                "value_status": "no_typed_value",
            },
        ]

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

    @pytest.mark.asyncio
    async def test_authoritative_outcome_not_demonstrated_rejects_extraction_schema_wrapper_mismatch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        label = "lookup_entry"
        child_paths = ["output.npi", "output.locations[].address", "output.statuses"]
        claimed_schema = (
            '{"type":"object","properties":{'
            '"npi":{"type":"string"},'
            '"locations":{"type":"array","items":{"type":"object","properties":{"address":{"type":"string"}}}},'
            '"statuses":{"type":"array","items":{"type":"string"}}}}'
        )
        terminal_schema = (
            '{"type":"object","properties":{"output":{"type":"object","properties":{'
            '"npi":{"type":"string"},'
            '"locations":{"type":"array","items":{"type":"object","properties":{"address":{"type":"string"}}}},'
            '"statuses":{"type":"array","items":{"type":"string"}}}}}}'
        )
        ctx.code_artifact_metadata = {
            label: {
                "block_label": label,
                "claimed_outcomes": [{"goal_value_paths": child_paths, "extraction_schema": claimed_schema}],
                "terminal_verifier_expectations": [
                    {"goal_value_paths": child_paths, "extraction_schema": terminal_schema}
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
                {"output_path": "output.npi", "output_root": "output", "value_status": "no_typed_value"},
                {
                    "output_path": "output.locations[].address",
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
                          "locations": [{{"address": "Example location"}}],
                          "statuses": ["active"],
                      }}
                  }}
            """
        )

        result = await _update_workflow({"workflow_yaml": candidate_yaml}, ctx, allow_missing_credentials=True)

        assert result["ok"] is False
        assert "extraction_schema" in result["error"]
        outcome = ctx.latest_recorded_build_test_outcome
        assert outcome is not None
        assert outcome.reason_code == "metadata_reject"
        assert outcome.missing_requested_output_facts == [
            {
                "output_path": "output.locations[].address",
                "output_root": "output",
                "reason_code": "recorded_outcome_missing_output_coverage",
                "value_status": "no_typed_value",
            },
            {
                "output_path": "output.npi",
                "output_root": "output",
                "reason_code": "recorded_outcome_missing_output_coverage",
                "value_status": "no_typed_value",
            },
            {
                "output_path": "output.statuses",
                "output_root": "output",
                "reason_code": "recorded_outcome_missing_output_coverage",
                "value_status": "no_typed_value",
            },
        ]
        repair_context = result["data"]["authoring_repair_context"]
        assert repair_context["reason_code"] == "metadata_reject"
        assert repair_context["block_label"] == label
        assert repair_context["runtime_failure_class"] == "recorded_outcome_missing_output_coverage"
        assert isinstance(ctx.last_code_authoring_repair_context, CodeAuthoringRepairContext)
        assert ctx.last_code_authoring_repair_context.model_dump(mode="json") == repair_context

    @pytest.mark.asyncio
    async def test_requested_output_contract_rejects_collapsed_candidate_before_recorded_outcome(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        label = "lookup_entry"
        ctx.request_policy = RequestPolicy(
            completion_criteria=[
                SimpleNamespace(
                    id="requested_value",
                    output_path="output.npi",
                    level="run",
                    method_mandated=False,
                    kind="outcome",
                ),
                SimpleNamespace(
                    id="requested_locations",
                    output_path="output.locations",
                    level="run",
                    method_mandated=False,
                    kind="outcome",
                ),
                SimpleNamespace(
                    id="requested_statuses",
                    output_path="output.statuses",
                    level="run",
                    method_mandated=False,
                    kind="outcome",
                ),
            ]
        )
        ctx.code_artifact_metadata = {
            label: {
                "block_label": label,
                "claimed_outcomes": [{"goal_value_paths": ["output"]}],
                "terminal_verifier_expectations": [{"goal_value_paths": ["output"]}],
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
                  return {{"output": {{"summary": "found"}}}}
            """
        )

        result = await _update_workflow({"workflow_yaml": candidate_yaml}, ctx, allow_missing_credentials=True)

        assert result["ok"] is False
        assert "requested output contract" in result["error"]
        outcome = ctx.latest_recorded_build_test_outcome
        assert outcome is not None
        assert outcome.reason_code == "metadata_reject"
        assert outcome.missing_requested_output_facts == [
            {
                "output_path": "output.locations",
                "output_root": "output",
                "reason_code": "requested_output_contract_missing_output_coverage",
                "value_status": "no_typed_value",
            },
            {
                "output_path": "output.npi",
                "output_root": "output",
                "reason_code": "requested_output_contract_missing_output_coverage",
                "value_status": "no_typed_value",
            },
            {
                "output_path": "output.statuses",
                "output_root": "output",
                "reason_code": "requested_output_contract_missing_output_coverage",
                "value_status": "no_typed_value",
            },
        ]
        repair_context = result["data"]["authoring_repair_context"]
        assert repair_context["reason_code"] == "metadata_reject"
        assert repair_context["block_label"] == label
        assert repair_context["runtime_failure_class"] == "requested_output_contract_missing_output_coverage"
        assert isinstance(ctx.last_code_authoring_repair_context, CodeAuthoringRepairContext)
        assert ctx.last_code_authoring_repair_context.model_dump(mode="json") == repair_context

    def test_single_output_contract_evaluator_reports_all_deficiency_classes(self) -> None:
        ctx = _code_only_ctx()
        ctx.scout_trajectory = [
            {"tool_name": "type_text", "selector": "#filter", "source_url": "https://example.com/records"},
            {"tool_name": "click", "selector": "#choose", "source_url": "https://example.com/records"},
        ]
        ctx.request_policy = RequestPolicy(
            completion_criteria=[
                SimpleNamespace(
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

    @pytest.mark.asyncio
    async def test_update_and_run_preflight_and_update_workflow_share_contract_signature(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        workflow_yaml = _yaml(
            """
            title: Entry lookup
            workflow_definition:
              blocks:
              - block_type: code
                label: extract_entry_output
                code: |
                  return {"output": {"summary": "found"}}
            """
        )
        repair_context = CodeAuthoringRepairContext(
            block_label="extract_entry_output",
            reason_code="metadata_reject",
            required_goal_value_paths=["output.record_id"],
            required_extraction_schema_paths=["output.record_id"],
            required_code_return_paths=["output.record_id"],
            metadata_contract_source="requested_output_contract",
            metadata_contract_reason_code="requested_output_contract_missing_output_coverage",
        )
        update_ctx = _code_only_ctx()
        update_ctx.turn_id = "shared-contract-turn"
        update_ctx.last_code_authoring_repair_context = repair_context
        run_ctx = _code_only_ctx()
        run_ctx.turn_id = "shared-contract-turn"
        run_ctx.last_code_authoring_repair_context = repair_context

        update_result = await _update_workflow(
            {"workflow_yaml": workflow_yaml}, update_ctx, allow_missing_credentials=True
        )
        run_result = workflow_update_module._metadata_contract_run_preflight_reject(run_ctx, workflow_yaml, [])

        assert run_result is not None
        assert update_result["ok"] is False
        assert run_result["ok"] is False
        assert (
            update_result["data"]["canonical_output_contract_signature"]
            == (run_result["data"]["canonical_output_contract_signature"])
        )
        assert (
            update_result["data"]["canonical_required_child_paths"]
            == (run_result["data"]["canonical_required_child_paths"])
        )
        assert update_result["data"]["satisfying_templates"] == run_result["data"]["satisfying_templates"]

    def test_static_return_uncertainty_is_run_preflight_advisory_only(self) -> None:
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
                  return build_output()
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
        assert run_eval.missing_return_paths == []
        assert run_eval.payload["static_return_advisory_paths"] == ["output.record_id"]
        assert run_eval.can_attempt_run is True

    def test_complete_metadata_without_code_return_is_run_preflight_advisory(self) -> None:
        ctx = _code_only_ctx()
        required_paths = {"output.account_number", "output.confirmation_number"}
        ctx.request_policy = RequestPolicy(
            completion_criteria=[
                SimpleNamespace(
                    id="requested_account",
                    output_path="output.account_number",
                    level="run",
                    method_mandated=False,
                    kind="outcome",
                ),
                SimpleNamespace(
                    id="requested_confirmation",
                    output_path="output.confirmation_number",
                    level="run",
                    method_mandated=False,
                    kind="outcome",
                ),
            ]
        )
        schema = workflow_update_module._schema_template_text_for_required_paths(required_paths)
        metadata = [
            {
                "block_label": "submit_service_request",
                "claimed_outcomes": [{"goal_value_paths": sorted(required_paths), "extraction_schema": schema}],
                "terminal_verifier_expectations": [
                    {"goal_value_paths": sorted(required_paths), "extraction_schema": schema}
                ],
            }
        ]
        workflow_yaml = _yaml(
            """
            title: Service request
            workflow_definition:
              blocks:
              - block_type: code
                label: submit_service_request
                code: |
                  await page.get_by_role("button", name="Submit").click()
            """
        )
        signature = workflow_update_module._output_contract_signature(
            ctx=ctx,
            workflow_yaml=workflow_yaml,
            source="requested_output_contract",
            reason_code="requested_output_contract_missing_output_coverage",
            required_paths=required_paths,
        )
        ctx.output_contract_reject_count_by_signature = {signature: 2}

        evaluation = workflow_update_module._evaluate_output_contract_for_code_block(
            ctx, workflow_yaml, metadata, allow_static_return_advisory=True
        )
        result = workflow_update_module._metadata_contract_run_preflight_reject(ctx, workflow_yaml, metadata)

        assert evaluation is not None
        assert evaluation.can_attempt_run is True
        assert evaluation.missing_return_paths == []
        assert evaluation.payload["static_return_advisory_paths"] == sorted(required_paths)
        assert evaluation.payload["post_steering_static_return_advisory"] is True
        assert result is None

    @pytest.mark.asyncio
    async def test_save_only_update_rejects_post_steering_static_return_gap(
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
        ctx.turn_id = "save-only-static-return-gap"
        signature = workflow_update_module._output_contract_signature(
            ctx=ctx,
            workflow_yaml=workflow_yaml,
            source="requested_output_contract",
            reason_code="requested_output_contract_missing_output_coverage",
            required_paths={"output.record_id"},
        )
        ctx.output_contract_reject_count_by_signature = {signature: 2}

        result = await _update_workflow(
            {"workflow_yaml": workflow_yaml},
            ctx,
            allow_missing_credentials=True,
        )

        assert result["ok"] is False
        assert result["data"]["reason_code"] == "output_contract_required"
        assert result["data"]["missing_code_return_paths"] == ["output.record_id"]
        assert result["data"]["static_return_advisory_paths"] == []

    @pytest.mark.asyncio
    async def test_run_path_allows_post_steering_declared_output_return_shape_gap(
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
        signature = workflow_update_module._output_contract_signature(
            ctx=ctx,
            workflow_yaml=workflow_yaml,
            source="requested_output_contract",
            reason_code="requested_output_contract_missing_output_coverage",
            required_paths={"output.record_id"},
        )
        ctx.output_contract_reject_count_by_signature = {signature: 2}

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
        monkeypatch.setattr(workflow_update_module, "composition_page_evidence_error", lambda *_args, **_kwargs: None)
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
        monkeypatch.setattr(workflow_update_module, "composition_page_evidence_error", lambda *_args, **_kwargs: None)
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

    @pytest.mark.asyncio
    async def test_output_contract_reject_budget_counts_same_signature_across_classes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        ctx.request_policy = RequestPolicy(
            completion_criteria=[
                SimpleNamespace(
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
              blocks:
              - block_type: code
                label: extract_entry_output
                code: |
                  return {"output": {"summary": "found"}}
            """
        )

        result = None
        for _ in range(4):
            result = await _update_workflow({"workflow_yaml": workflow_yaml}, ctx, allow_missing_credentials=True)

        assert result is not None
        assert result["ok"] is False
        assert result["data"]["reason_code"] == "output_contract_required"
        assert result["data"]["output_contract_reject_count"] == 4
        assert result["data"]["canonical_required_child_paths"] == ["output.record_id"]

    def test_output_contract_signature_uses_stable_scope_and_required_path_identity(self) -> None:
        ctx = _code_only_ctx()
        ctx.turn_id = "contract-scope-a"
        first = workflow_update_module._output_contract_signature(
            ctx=ctx,
            workflow_yaml=_yaml(
                """
                title: First draft title
                workflow_definition:
                  blocks: []
                """
            ),
            source="requested_output_contract",
            reason_code="requested_output_contract_missing_output_coverage",
            required_paths={"output.locations[].address", "output.statuses"},
        )
        second = workflow_update_module._output_contract_signature(
            ctx=ctx,
            workflow_yaml=_yaml(
                """
                title: Changed draft title
                workflow_definition:
                  blocks: []
                """
            ),
            source="runtime_output_repair",
            reason_code="runtime_output_repair_required",
            required_paths={"output.statuses", "output.locations[].address"},
        )
        other_ctx = _code_only_ctx()
        other_ctx.turn_id = "contract-scope-b"
        other_scope = workflow_update_module._output_contract_signature(
            ctx=other_ctx,
            workflow_yaml=_yaml(
                """
                title: Changed draft title
                workflow_definition:
                  blocks: []
                """
            ),
            source="runtime_output_repair",
            reason_code="runtime_output_repair_required",
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

    def test_output_contract_family_counter_aggregates_reject_classes(self) -> None:
        ctx = _code_only_ctx()
        ctx.turn_id = "contract-budget-scope"
        required_paths = {"output.record_id"}
        first = workflow_update_module._record_output_contract_family_reject(
            ctx,
            required_paths,
            reject_family="metadata_reject",
        )
        second = workflow_update_module._record_output_contract_family_reject(
            ctx,
            required_paths,
            reject_family="synthesized_parameter_binding_ambiguous",
        )
        third = workflow_update_module._record_output_contract_family_reject(
            ctx,
            required_paths,
            reject_family="declared_output_return_shape",
        )

        signature = workflow_update_module._output_contract_signature(
            ctx=ctx,
            workflow_yaml="title: Any\nworkflow_definition:\n  blocks: []\n",
            source="different",
            reason_code="different",
            required_paths=required_paths,
        )
        assert (first, second, third) == (1, 2, 3)
        assert ctx.output_contract_reject_count_by_signature[signature] == 3

    def test_output_contract_family_counter_is_scoped(self) -> None:
        ctx = _code_only_ctx()
        required_paths = {"output.status"}

        ctx.turn_id = "first-build-goal"
        first_scope_count = workflow_update_module._record_output_contract_family_reject(
            ctx,
            required_paths,
            reject_family="metadata_reject",
        )
        ctx.turn_id = "second-build-goal"
        second_scope_count = workflow_update_module._record_output_contract_family_reject(
            ctx,
            required_paths,
            reject_family="metadata_reject",
        )
        ctx.turn_id = "first-build-goal"
        first_scope_again = workflow_update_module._record_output_contract_family_reject(
            ctx,
            required_paths,
            reject_family="output_contract_required",
        )

        assert (first_scope_count, second_scope_count, first_scope_again) == (1, 1, 2)
        assert len(ctx.output_contract_reject_count_by_signature) == 2

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

    def test_metadata_contract_recorded_key_stable_across_reject_count_and_reason_drift(self) -> None:
        ctx = _code_only_ctx()
        ctx.turn_id = "stable-contract-key"
        ctx.request_policy = RequestPolicy(
            completion_criteria=[
                SimpleNamespace(
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
            title: First candidate title
            workflow_definition:
              blocks:
              - block_type: code
                label: extract_entry_output
                code: |
                  return {"output": {"summary": "found"}}
            """
        )
        evaluation = workflow_update_module._evaluate_output_contract_for_code_block(ctx, workflow_yaml, [])
        assert evaluation is not None

        workflow_update_module._record_output_contract_reject(
            ctx,
            evaluation,
            summary="First reject wording.",
        )
        first_key = ctx.latest_recorded_build_test_outcome.structural_key
        assert first_key is not None

        evaluation.payload["reason_code"] = "declared_output_return_shape"
        workflow_update_module._record_output_contract_reject(
            ctx,
            evaluation,
            summary="Changed reject wording and reason site.",
        )

        assert ctx.latest_recorded_build_test_outcome.structural_key == first_key
        assert latest_recorded_build_test_outcome_repeated(ctx) is True
        assert ctx.latest_recorded_build_test_outcome.missing_requested_output_facts

    def test_metadata_contract_recorded_key_keeps_distinct_scope_and_required_paths(self) -> None:
        workflow_yaml = _yaml(
            """
            title: Shared shape
            workflow_definition:
              blocks:
              - block_type: code
                label: extract_entry_output
                code: |
                  return {"output": {"summary": "found"}}
            """
        )

        def recorded_key(turn_id: str, output_path: str) -> str:
            ctx = _code_only_ctx()
            ctx.turn_id = turn_id
            ctx.request_policy = RequestPolicy(
                completion_criteria=[
                    SimpleNamespace(
                        id="requested_value",
                        output_path=output_path,
                        level="run",
                        method_mandated=False,
                        kind="outcome",
                    )
                ]
            )
            evaluation = workflow_update_module._evaluate_output_contract_for_code_block(ctx, workflow_yaml, [])
            assert evaluation is not None
            workflow_update_module._record_output_contract_reject(ctx, evaluation, summary="Rejected.")
            key = ctx.latest_recorded_build_test_outcome.structural_key
            assert key is not None
            return key

        first_scope_key = recorded_key("first-build-goal", "output.status")
        second_scope_key = recorded_key("second-build-goal", "output.status")
        other_path_key = recorded_key("first-build-goal", "output.npi")

        assert first_scope_key != second_scope_key
        assert first_scope_key != other_path_key

    @pytest.mark.asyncio
    async def test_output_contract_after_two_steering_cycles_imposes_keyed_return_envelope(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        ctx.request_policy = RequestPolicy(
            completion_criteria=[
                SimpleNamespace(
                    id="requested_value",
                    output_path="output.record_id",
                    level="run",
                    method_mandated=False,
                    kind="outcome",
                ),
                SimpleNamespace(
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

        first = await _update_workflow({"workflow_yaml": workflow_yaml}, ctx, allow_missing_credentials=True)
        second = await _update_workflow({"workflow_yaml": workflow_yaml}, ctx, allow_missing_credentials=True)
        imposed = await _update_workflow({"workflow_yaml": workflow_yaml}, ctx, allow_missing_credentials=True)

        assert first["ok"] is False
        assert second["ok"] is False
        assert imposed["ok"] is True
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

    @pytest.mark.asyncio
    async def test_requested_output_contract_omits_repair_context_when_output_target_is_ambiguous(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        ctx.request_policy = RequestPolicy(
            completion_criteria=[
                SimpleNamespace(
                    id="requested_value",
                    output_path="output.npi",
                    level="run",
                    method_mandated=False,
                    kind="outcome",
                )
            ]
        )
        ctx.code_artifact_metadata = {
            "lookup_entry": {
                "block_label": "lookup_entry",
                "claimed_outcomes": [{"goal_value_paths": ["output"]}],
                "terminal_verifier_expectations": [{"goal_value_paths": ["output"]}],
            }
        }
        ctx.workflow_verification_evidence.code_artifact_metadata = dict(ctx.code_artifact_metadata)
        candidate_yaml = _yaml(
            """
            title: Entry lookup
            workflow_definition:
              blocks:
              - block_type: code
                label: open_entry
                code: |
                  await page.goto("https://example.com/search")
              - block_type: code
                label: lookup_entry
                code: |
                  return {"output": {"summary": "found"}}
            """
        )

        result = await _update_workflow({"workflow_yaml": candidate_yaml}, ctx, allow_missing_credentials=True)

        assert result["ok"] is False
        assert "requested output contract" in result["error"]
        assert "authoring_repair_context" not in result["data"]
        assert ctx.last_code_authoring_repair_context is None
        outcome = ctx.latest_recorded_build_test_outcome
        assert outcome is not None
        assert outcome.reason_code == "metadata_reject"
        assert outcome.missing_requested_output_facts == [
            {
                "output_path": "output.npi",
                "output_root": "output",
                "reason_code": "requested_output_contract_missing_output_coverage",
                "value_status": "no_typed_value",
            }
        ]
        assert result["data"]["metadata_repair_contract"] is None
        assert result["data"]["block_label"] == ""
        assert result["data"]["shape_violations"] == ["missing_output_owner"]

    @pytest.mark.asyncio
    async def test_requested_output_contract_omits_repair_context_when_schema_target_is_ambiguous(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        child_paths = ["output.npi"]
        ctx.request_policy = RequestPolicy(
            completion_criteria=[
                SimpleNamespace(
                    id="requested_value",
                    output_path="output.npi",
                    level="run",
                    method_mandated=False,
                    kind="outcome",
                )
            ]
        )
        ctx.code_artifact_metadata = {
            "lookup_entry": {
                "block_label": "lookup_entry",
                "claimed_outcomes": [
                    {
                        "goal_value_paths": child_paths,
                        "extraction_schema": '{"type":"object","properties":{"npi":{"type":"string"}}}',
                    }
                ],
                "terminal_verifier_expectations": [{"goal_value_paths": child_paths}],
            }
        }
        ctx.workflow_verification_evidence.code_artifact_metadata = dict(ctx.code_artifact_metadata)
        candidate_yaml = _yaml(
            """
            title: Entry lookup
            workflow_definition:
              blocks:
              - block_type: code
                label: open_entry
                code: |
                  await page.goto("https://example.com/search")
              - block_type: code
                label: lookup_entry
                code: |
                  return {"output": {"npi": "1234567890"}}
            """
        )

        result = await _update_workflow({"workflow_yaml": candidate_yaml}, ctx, allow_missing_credentials=True)

        assert result["ok"] is False
        assert "extraction_schema" in result["error"]
        assert result["data"]["authoring_repair_context"]["block_label"] == "lookup_entry"
        assert ctx.last_code_authoring_repair_context is not None
        outcome = ctx.latest_recorded_build_test_outcome
        assert outcome is not None
        assert outcome.reason_code == "metadata_reject"
        assert outcome.missing_requested_output_facts == [
            {
                "output_path": "output.npi",
                "output_root": "output",
                "reason_code": "requested_output_contract_missing_output_coverage",
                "value_status": "no_typed_value",
            }
        ]
        assert result["data"]["metadata_repair_contract"]["block_label"] == "lookup_entry"

    def test_metadata_contract_run_preflight_blocks_missing_metadata_before_run(self) -> None:
        ctx = _code_only_ctx()
        ctx.last_code_authoring_repair_context = CodeAuthoringRepairContext(
            block_label="extract_entry_output",
            reason_code="metadata_reject",
            required_goal_value_paths=["output.record_id", "output.flags"],
            required_extraction_schema_paths=["output.record_id", "output.flags"],
            required_code_return_paths=["output.record_id", "output.flags"],
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
                  return {"output": {"summary": "found"}}
            """
        )

        result = workflow_update_module._metadata_contract_run_preflight_reject(ctx, workflow_yaml, [])

        assert result is not None
        assert result["ok"] is False
        assert result["data"]["reason_code"] == "metadata_contract_required_before_run"
        assert result["data"]["output_contract_reason_code"] == "output_contract_required"
        assert result["data"]["block_label"] == "extract_entry_output"
        assert result["data"]["canonical_required_child_paths"] == ["output.flags", "output.record_id"]
        assert result["data"]["missing_goal_value_paths"] == ["output.flags", "output.record_id"]
        assert result["data"]["missing_extraction_schema_paths"] == ["output.flags", "output.record_id"]
        assert result["data"]["missing_code_return_paths"] == ["output.flags", "output.record_id"]
        assert result["data"]["metadata_contract_source"] == "requested_output_contract"
        assert result["data"]["metadata_contract_reason_code"] == ("requested_output_contract_missing_output_coverage")
        assert result["data"]["metadata_repair_contract"] == {
            "block_label": "extract_entry_output",
            "required_goal_value_paths": ["output.flags", "output.record_id"],
            "required_extraction_schema_paths": ["output.flags", "output.record_id"],
            "required_code_return_paths": ["output.flags", "output.record_id"],
            "source": "requested_output_contract",
            "reason_code": "requested_output_contract_missing_output_coverage",
        }
        assert result["data"]["authoring_repair_context"]["required_goal_value_paths"] == [
            "output.flags",
            "output.record_id",
        ]
        assert result["data"]["missing_requested_output_facts"] == [
            {
                "output_path": "output.flags",
                "output_root": "output",
                "reason_code": "requested_output_contract_missing_output_coverage",
                "value_status": "no_typed_value",
            },
            {
                "output_path": "output.record_id",
                "output_root": "output",
                "reason_code": "requested_output_contract_missing_output_coverage",
                "value_status": "no_typed_value",
            },
        ]
        sanitized = sanitize_tool_result_for_llm("update_and_run_blocks", result)
        assert sanitized["data"]["authoring_repair_context"]["metadata_contract_source"] == (
            "requested_output_contract"
        )
        assert sanitized["data"]["metadata_repair_contract"] == result["data"]["metadata_repair_contract"]

    def test_metadata_contract_run_preflight_preserves_run_backed_convergence_terminal(self) -> None:
        ctx = _code_only_ctx()
        ctx.request_policy = RequestPolicy(
            completion_criteria=[
                SimpleNamespace(
                    id="requested_account",
                    output_path="output.account_number",
                    level="run",
                    method_mandated=False,
                    kind="outcome",
                )
            ]
        )
        workflow_yaml = _yaml(
            """
            title: Service request
            workflow_definition:
              blocks:
              - block_type: code
                label: submit_service_request
                code: |
                  await page.get_by_role("button", name="Submit").click()
            """
        )
        metadata = [
            {
                "artifact_id": "code_artifact:submit_service_request",
                "block_label": "submit_service_request",
                "claimed_outcomes": [
                    {
                        "goal_value_paths": ["output.account_number"],
                        "extraction_schema": (
                            '{"type":"object","properties":{"output":{"type":"object","properties":'
                            '{"account_number":{}}}}}'
                        ),
                    }
                ],
            }
        ]
        ctx.latest_recorded_build_test_outcome = RecordedBuildTestOutcome(
            phase="persisted_block_run",
            attempted_tool="update_and_run_blocks",
            verdict="repairable_failure",
            reason_code="outcome_not_demonstrated",
            workflow_run_id="wr_recorded",
            block_labels=["submit_service_request"],
            structural_failure_identity="completion:typed-output",
            authored_structure_signature="previous-authoring-signature",
        )
        ctx.recorded_persisted_block_run_workflow_run_id = "wr_recorded"
        recorded_sig = authored_block_signatures_from_workflow(workflow_yaml, metadata)["submit_service_request"]
        ctx.recorded_outcome_binding_constraint = RecordedOutcomeBindingConstraint(
            repeated_structural_key=ctx.latest_recorded_build_test_outcome.structural_key or "",
            phase="persisted_block_run",
            reason_code="outcome_not_demonstrated",
            frontier_facet="value_shape",
            owning_block_labels=["submit_service_request"],
            diagnostic_reason="empty_page",
            workflow_run_id="wr_recorded",
            recorded_block_signatures={"submit_service_request": recorded_sig},
        )

        result = workflow_update_module._metadata_contract_run_preflight_reject(ctx, workflow_yaml, metadata)

        assert result is not None
        assert result["ok"] is False
        assert "frontier" in result["error"]
        assert ctx.turn_halt is not None
        assert ctx.turn_halt.kind is TurnHaltKind.REPAIR_CEILING_REACHED
        outcome = ctx.latest_recorded_build_test_outcome
        assert outcome is not None
        assert outcome.reason_code == "unchanged_after_recorded_outcome"
        assert outcome.workflow_run_id is None

    def test_metadata_contract_run_preflight_budget_remains_repair_before_run(self) -> None:
        ctx = _code_only_ctx()
        ctx.turn_id = "preflight-no-run-contract"
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
                  return {"output": {"summary": "found"}}
            """
        )

        results = [
            workflow_update_module._metadata_contract_run_preflight_reject(ctx, workflow_yaml, []) for _ in range(4)
        ]

        assert results[0] is not None
        assert results[0]["ok"] is False
        assert results[0]["data"]["reason_code"] == "metadata_contract_required_before_run"
        assert results[0]["data"]["output_contract_reason_code"] == "output_contract_required"
        assert results[0]["data"]["output_contract_reject_count"] == 1
        assert results[-1] is None
        assert all(
            result is None or result["data"]["reason_code"] != "output_contract_reject_budget_exhausted"
            for result in results
        )

    def test_metadata_contract_run_preflight_accepts_complete_submitted_metadata(self) -> None:
        ctx = _code_only_ctx()
        ctx.last_code_authoring_repair_context = CodeAuthoringRepairContext(
            block_label="extract_entry_output",
            reason_code="metadata_reject",
            required_goal_value_paths=["output.record_id", "output.flags"],
            required_extraction_schema_paths=["output.record_id", "output.flags"],
            required_code_return_paths=["output.record_id", "output.flags"],
            metadata_contract_source="recorded_outcome",
            metadata_contract_reason_code="recorded_outcome_missing_output_coverage",
        )
        schema = (
            '{"type":"object","properties":{"output":{"type":"object","properties":{'
            '"record_id":{"type":"string"},'
            '"flags":{"type":"array","items":{"type":"string"}}}}}}'
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
        metadata = [
            {
                "block_label": "extract_entry_output",
                "claimed_outcomes": [
                    {"goal_value_paths": ["output.record_id", "output.flags"], "extraction_schema": schema}
                ],
                "terminal_verifier_expectations": [
                    {"goal_value_paths": ["output.record_id", "output.flags"], "extraction_schema": schema}
                ],
            }
        ]

        result = workflow_update_module._metadata_contract_run_preflight_reject(ctx, workflow_yaml, metadata)

        assert result is None

    def test_metadata_contract_run_preflight_rejects_root_only_output(self) -> None:
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
                  return {"output": {"summary": "found"}}
            """
        )
        metadata = [
            {
                "block_label": "extract_entry_output",
                "claimed_outcomes": [
                    {
                        "goal_value_paths": ["output"],
                        "extraction_schema": '{"type":"object","properties":{"output":{"type":"object"}}}',
                    }
                ],
                "terminal_verifier_expectations": [{"goal_value_paths": ["output"]}],
            }
        ]

        result = workflow_update_module._metadata_contract_run_preflight_reject(ctx, workflow_yaml, metadata)

        assert result is not None
        assert result["ok"] is False
        assert result["data"]["missing_goal_value_paths"] == ["output.record_id"]
        assert result["data"]["missing_extraction_schema_paths"] == ["output.record_id"]
        assert result["data"]["missing_code_return_paths"] == ["output.record_id"]

    def test_metadata_contract_scaffold_uses_recorded_paths_before_request_policy(self) -> None:
        ctx = _code_only_ctx()
        ctx.request_policy = RequestPolicy(
            completion_criteria=[
                SimpleNamespace(
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

    def test_independent_judgment_repair_context_is_not_rehydrated_as_code_return_path(self) -> None:
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
            block_label="judge_login_gate_blocks_target",
            reason_code="metadata_reject",
            required_goal_value_paths=["output.login_gate_blocks_target"],
            required_extraction_schema_paths=["output.login_gate_blocks_target"],
            required_code_return_paths=["output.login_gate_blocks_target"],
            metadata_contract_source="requested_output_contract",
            metadata_contract_reason_code="requested_output_contract_missing_output_coverage",
        )

        required_paths = workflow_update_module._output_contract_required_paths_source(ctx).union
        result = workflow_update_module._metadata_contract_run_preflight_reject(ctx, _SAFE_CODE_YAML, [])

        assert required_paths == set()
        assert result is None

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
                SimpleNamespace(
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
    async def test_runtime_output_repair_facts_trigger_one_envelope_attempt_before_budget(
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
                SimpleNamespace(
                    id="requested_value",
                    output_path="output.record_id",
                    level="run",
                    method_mandated=False,
                    kind="outcome",
                ),
                SimpleNamespace(
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
                SimpleNamespace(
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
    async def test_separated_spine_shape_rejects_collapsed_output_owner(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        ctx.scout_trajectory = [
            {"tool_name": "type_text", "selector": "#filter", "source_url": "https://example.com/records"},
            {"tool_name": "click", "selector": "#choose", "source_url": "https://example.com/records"},
        ]
        ctx.request_policy = RequestPolicy(
            completion_criteria=[
                SimpleNamespace(
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

        assert result["ok"] is False
        assert result["data"]["reason_code"] == "output_contract_required"
        assert result["data"]["shape_violations"] == ["separated_spine_shape_required"]

    @pytest.mark.asyncio
    async def test_separated_spine_shape_rejects_collapsed_output_owner_with_sibling_block(
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
                SimpleNamespace(
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
                label: prepare_session
                code: |
                  context["ready"] = True
              - block_type: code
                label: extract_record
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

        assert result["ok"] is False
        assert result["data"]["reason_code"] == "output_contract_required"
        assert result["data"]["shape_violations"] == ["separated_spine_shape_required"]
        assert result["data"]["block_label"] == "extract_record"
        assert result["data"]["output_owner_labels"] == ["extract_record"]

    @pytest.mark.asyncio
    async def test_separated_spine_shape_rejects_collapsed_output_owner_with_extra_browser_action(
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
                SimpleNamespace(
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
                  await page.locator("#extra").click()
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

        assert result["ok"] is False
        assert result["data"]["reason_code"] == "output_contract_required"
        assert result["data"]["shape_violations"] == ["separated_spine_shape_required"]
        assert result["data"]["block_label"] == "extract_record"

    @pytest.mark.asyncio
    async def test_separated_spine_shape_ignores_one_block_without_multi_stage_spine(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        ctx.scout_trajectory = []
        ctx.request_policy = RequestPolicy(
            completion_criteria=[
                SimpleNamespace(
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
                SimpleNamespace(
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
    async def test_requested_output_contract_missing_metadata_records_child_path_facts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        ctx.request_policy = RequestPolicy(
            completion_criteria=[
                SimpleNamespace(
                    id="requested_value",
                    output_path="output.npi",
                    level="run",
                    method_mandated=False,
                    kind="outcome",
                ),
                SimpleNamespace(
                    id="requested_statuses",
                    output_path="output.statuses",
                    level="run",
                    method_mandated=False,
                    kind="outcome",
                ),
            ]
        )
        candidate_yaml = _yaml(
            """
            title: Entry lookup
            workflow_definition:
              parameters:
              - {parameter_type: output, key: lookup_result}
              blocks:
              - block_type: code
                label: extract_entry_output
                prompt: Extract structured fields from the entry.
                code: |
                  await page.goto("https://example.com/search")
                  return {"output": {"summary": "found"}}
            """
        )

        result = await _update_workflow({"workflow_yaml": candidate_yaml}, ctx, allow_missing_credentials=True)

        assert result["ok"] is False
        assert "Required requested output paths: output.npi, output.statuses" in result["error"]
        expected_facts = [
            {
                "output_path": "output.npi",
                "output_root": "output",
                "reason_code": "requested_output_contract_missing_output_coverage",
                "value_status": "no_typed_value",
            },
            {
                "output_path": "output.statuses",
                "output_root": "output",
                "reason_code": "requested_output_contract_missing_output_coverage",
                "value_status": "no_typed_value",
            },
        ]
        outcome = ctx.latest_recorded_build_test_outcome
        assert outcome is not None
        assert outcome.reason_code == "metadata_reject"
        assert outcome.missing_requested_output_facts == expected_facts
        assert result["data"]["missing_requested_output_facts"] == expected_facts
        assert result["data"]["metadata_repair_contract"] == {
            "block_label": "extract_entry_output",
            "required_goal_value_paths": ["output.npi", "output.statuses"],
            "required_extraction_schema_paths": ["output.npi", "output.statuses"],
            "required_code_return_paths": ["output.npi", "output.statuses"],
            "source": "requested_output_contract",
            "reason_code": "requested_output_contract_missing_output_coverage",
        }
        repair_context = result["data"]["authoring_repair_context"]
        assert repair_context["reason_code"] == "metadata_reject"
        assert repair_context["block_label"] == "extract_entry_output"
        assert repair_context["runtime_failure_class"] == "requested_output_contract_missing_output_coverage"
        assert repair_context["required_goal_value_paths"] == ["output.npi", "output.statuses"]
        assert repair_context["required_extraction_schema_paths"] == ["output.npi", "output.statuses"]
        assert repair_context["required_code_return_paths"] == ["output.npi", "output.statuses"]
        assert repair_context["metadata_contract_source"] == "requested_output_contract"
        assert repair_context["metadata_contract_reason_code"] == ("requested_output_contract_missing_output_coverage")
        assert isinstance(ctx.last_code_authoring_repair_context, CodeAuthoringRepairContext)
        assert ctx.last_code_authoring_repair_context.model_dump(mode="json") == repair_context
        sanitized = sanitize_tool_result_for_llm("update_workflow", result)
        assert sanitized["data"]["authoring_repair_context"]["required_goal_value_paths"] == [
            "output.npi",
            "output.statuses",
        ]
        assert sanitized["data"]["authoring_repair_context"]["required_extraction_schema_paths"] == [
            "output.npi",
            "output.statuses",
        ]
        assert sanitized["data"]["authoring_repair_context"]["required_code_return_paths"] == [
            "output.npi",
            "output.statuses",
        ]
        assert sanitized["data"]["authoring_repair_context"]["metadata_contract_source"] == "requested_output_contract"
        assert sanitized["data"]["metadata_repair_contract"] == result["data"]["metadata_repair_contract"]

        schema = (
            '{"type":"object","properties":{"output":{"type":"object","properties":{'
            '"npi":{"type":"string"},'
            '"statuses":{"type":"array","items":{"type":"string"}}}}}}'
        )
        corrected_metadata = {
            "extract_entry_output": {
                "block_label": "extract_entry_output",
                "claimed_outcomes": [
                    {"goal_value_paths": ["output.npi", "output.statuses"], "extraction_schema": schema}
                ],
                "terminal_verifier_expectations": [
                    {"goal_value_paths": ["output.npi", "output.statuses"], "extraction_schema": schema}
                ],
            }
        }
        corrected_yaml = _yaml(
            """
            title: Entry lookup
            workflow_definition:
              parameters:
              - {parameter_type: output, key: lookup_result}
              blocks:
              - block_type: code
                label: extract_entry_output
                prompt: Extract structured fields from the entry.
                code: |
                  await page.goto("https://example.com/search")
                  return {"output": {"npi": "1234567890", "statuses": ["active"]}}
            """
        )

        corrected = await _update_workflow(
            {"workflow_yaml": corrected_yaml, "code_artifact_metadata": corrected_metadata},
            ctx,
            allow_missing_credentials=True,
        )

        assert corrected["ok"] is True
        accepted_yaml = parse_workflow_yaml(ctx.workflow_yaml)
        accepted_block = _single_code_block(accepted_yaml)
        assert accepted_block["label"] == "extract_entry_output"
        assert (
            accepted_block["code"].strip().endswith('return {"output": {"npi": "1234567890", "statuses": ["active"]}}')
        )
        assert accepted_block["extraction_schema"] == schema
        stored_metadata = ctx.code_artifact_metadata["extract_entry_output"]
        assert stored_metadata["block_label"] == corrected_metadata["extract_entry_output"]["block_label"]
        assert stored_metadata["claimed_outcomes"][0]["goal_value_paths"] == ["output.npi", "output.statuses"]
        assert stored_metadata["claimed_outcomes"][0]["extraction_schema"] == schema
        assert stored_metadata["terminal_verifier_expectations"][0]["goal_value_paths"] == [
            "output.npi",
            "output.statuses",
        ]
        assert stored_metadata["terminal_verifier_expectations"][0]["extraction_schema"] == schema
        assert ctx.workflow_verification_evidence.code_artifact_metadata == ctx.code_artifact_metadata

    @pytest.mark.asyncio
    async def test_recorded_outcome_missing_metadata_records_recorded_child_path_facts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        ctx.request_policy = RequestPolicy(
            completion_criteria=[
                SimpleNamespace(
                    id="requested_value",
                    output_path="output.requested_only",
                    level="run",
                    method_mandated=False,
                    kind="outcome",
                ),
            ]
        )
        ctx.latest_recorded_build_test_outcome = RecordedBuildTestOutcome(
            phase="persisted_block_run",
            attempted_tool="update_and_run_blocks",
            verdict="repairable_failure",
            reason_code="outcome_not_demonstrated",
            structural_failure_identity="completion:unsatisfied-output",
            authored_structure_signature="authored:previous-failed-candidate",
            missing_requested_output_facts=[
                {"output_path": "output.recorded", "output_root": "output", "value_status": "no_typed_value"},
            ],
        )
        candidate_yaml = _yaml(
            """
            title: Entry lookup
            workflow_definition:
              parameters:
              - {parameter_type: output, key: lookup_result}
              blocks:
              - block_type: code
                label: extract_entry_output
                prompt: Extract structured fields from the entry.
                code: |
                  await page.goto("https://example.com/search")
                  return {"output": {"summary": "found"}}
            """
        )

        result = await _update_workflow({"workflow_yaml": candidate_yaml}, ctx, allow_missing_credentials=True)

        assert result["ok"] is False
        assert "Required requested output paths: output.recorded" in result["error"]
        outcome = ctx.latest_recorded_build_test_outcome
        assert outcome is not None
        assert outcome.reason_code == "metadata_reject"
        assert outcome.missing_requested_output_facts == [
            {
                "output_path": "output.recorded",
                "output_root": "output",
                "reason_code": "recorded_outcome_missing_output_coverage",
                "value_status": "no_typed_value",
            }
        ]
        assert result["data"]["missing_requested_output_facts"] == outcome.missing_requested_output_facts
        assert result["data"]["metadata_repair_contract"] == {
            "block_label": "extract_entry_output",
            "required_goal_value_paths": ["output.recorded"],
            "required_extraction_schema_paths": ["output.recorded"],
            "required_code_return_paths": ["output.recorded"],
            "source": "recorded_outcome",
            "reason_code": "recorded_outcome_missing_output_coverage",
        }
        repair_context = result["data"]["authoring_repair_context"]
        assert repair_context["reason_code"] == "metadata_reject"
        assert repair_context["block_label"] == "extract_entry_output"
        assert repair_context["runtime_failure_class"] == "recorded_outcome_missing_output_coverage"
        assert repair_context["required_goal_value_paths"] == ["output.recorded"]
        assert repair_context["required_extraction_schema_paths"] == ["output.recorded"]
        assert repair_context["required_code_return_paths"] == ["output.recorded"]
        assert repair_context["metadata_contract_source"] == "recorded_outcome"
        assert repair_context["metadata_contract_reason_code"] == "recorded_outcome_missing_output_coverage"
        assert isinstance(ctx.last_code_authoring_repair_context, CodeAuthoringRepairContext)
        assert ctx.last_code_authoring_repair_context.model_dump(mode="json") == repair_context

    @pytest.mark.asyncio
    async def test_authoritative_outcome_not_demonstrated_rejects_when_only_last_run_produced_required_roots(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        label = "extract_top_hn_post"
        ctx.verified_block_outputs[label] = {
            "top_post": "Claude Sonnet 5",
            "rank": 1,
            "url": "https://news.ycombinator.com/item?id=1",
        }
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
                  return {{"rank": 1, "url": "https://news.ycombinator.com/item?id=1"}}
            """
        )

        result = await _update_workflow({"workflow_yaml": candidate_yaml}, ctx, allow_missing_credentials=True)

        assert result["ok"] is False
        assert "does not cover the missing requested output paths" in result["error"]
        outcome = ctx.latest_recorded_build_test_outcome
        assert outcome is not None
        assert outcome.reason_code == "metadata_reject"
        assert outcome.missing_requested_output_facts == [
            {
                "output_path": "top_post",
                "output_root": "top_post",
                "reason_code": "recorded_outcome_missing_output_coverage",
                "value_status": "no_typed_value",
            }
        ]

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
    async def test_authoritative_outcome_not_demonstrated_still_rejects_empty_last_run_roots(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        label = "lookup_provider_and_extract_credentials"
        ctx.verified_block_outputs[label] = {
            "address": "",
            "credentialing_status": "",
            "locations": [],
            "statuses": [],
        }
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
                  await page.locator("#locInput").wait_for(state="visible", timeout=15000)
            """
        )

        result = await _update_workflow({"workflow_yaml": candidate_yaml}, ctx, allow_missing_credentials=True)

        assert result["ok"] is False
        assert "does not return any keyed output" in result["error"]

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
    async def test_captured_select_option_rejects_authored_text_click_plan_selection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        ctx.scout_trajectory = [
            {
                "tool_name": "select_option",
                "selector": "#planSelect",
                "source_url": "https://example.com/plans?session=private",
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
                  main = page.locator("main")
                  await main.get_by_text(str(plan), exact=True).first.click()
                  return {"selected_plan": str(plan)}
            """
        )

        result = await _update_workflow({"workflow_yaml": submitted}, ctx, allow_missing_credentials=True)

        assert result["ok"] is False
        assert "select-option interaction with a text click" in result["error"]
        repair_context = result["data"]["authoring_repair_context"]
        assert repair_context["reason_code"] == "select_option_interaction_mismatch"
        assert repair_context["block_label"] == "plan_selection"
        assert repair_context["selector"] == "#planSelect"
        assert repair_context["source_url"] == "https://example.com"
        assert "private" not in str(repair_context)
        assert ctx.last_code_authoring_repair_context is not None
        assert ctx.last_code_authoring_repair_context.reason_code == "select_option_interaction_mismatch"
        assert ctx.latest_recorded_build_test_outcome is not None
        assert ctx.latest_recorded_build_test_outcome.reason_code == "code_safety_reject"
        assert ctx.latest_recorded_build_test_outcome.is_authoritative is True
        assert ctx.has_staged_proposal is False

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
    async def test_runtime_hidden_option_failure_rejects_repeated_text_click_structure(
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
        ctx.latest_recorded_build_test_outcome = RecordedBuildTestOutcome(
            phase="persisted_block_run",
            attempted_tool="update_and_run_blocks",
            verdict="repairable_failure",
            reason_code="runtime_block_failure",
            structural_failure_identity="runtime:hidden-option-click",
            authored_structure_signature="authored:previous-hidden-option-click",
        )

        def submitted_yaml(return_value: str) -> str:
            return _yaml(
                f"""
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
                      main = page.locator("main")
                      await main.get_by_text(str(plan), exact=True).first.click()
                      return {{"selected_plan": "{return_value}"}}
                """
            )

        first = await _update_workflow({"workflow_yaml": submitted_yaml("first")}, ctx, allow_missing_credentials=True)
        first_key = ctx.latest_recorded_build_test_outcome.structural_key
        first_count = ctx.code_authoring_guardrail_reject_count
        second = await _update_workflow(
            {"workflow_yaml": submitted_yaml("second")}, ctx, allow_missing_credentials=True
        )

        assert first["ok"] is False
        assert second["ok"] is False
        assert first_key is not None
        assert ctx.latest_recorded_build_test_outcome.structural_key == first_key
        assert first_count == 1
        assert ctx.code_authoring_guardrail_reject_count == 2
        assert ctx.has_staged_proposal is False

    @pytest.mark.asyncio
    async def test_raw_conflict_marker_reject_carries_progress_surface_kind(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        result = await _update_workflow({"workflow_yaml": f"<<<<<<< HEAD\n{_SAFE_CODE_YAML}"}, ctx)
        assert result["ok"] is False
        assert result["data"]["surface_kind"] == "code_repair_progress"
        assert result["data"]["progress_text"]

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
            SimpleNamespace(
                id="requested_value",
                output_path="output.record_id",
                level="run",
                method_mandated=False,
                kind="outcome",
            )
        ]
    )
    signature = workflow_update_module._stable_output_contract_key("turn:t-spine", {"output.record_id"})
    ctx.output_contract_reject_count_by_signature = {
        signature: workflow_update_module._MAX_OUTPUT_CONTRACT_STEERING_REJECTS
    }
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

    def test_preflight_reject_persists_directive_repair_context(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(workflow_update_module, "synthesize_code_block", lambda *a, **k: _fake_spine_synthesized())
        ctx = _spine_actuation_ctx()
        workflow_yaml = _collapsed_spine_yaml(
            "_setup = 1\n" + _SPINE_SYNTH_CODE + '\nreturn {"output": {"record_id": "X"}}'
        )

        result = workflow_update_module._metadata_contract_run_preflight_reject(ctx, workflow_yaml, [])

        assert result is not None
        assert result["ok"] is False
        assert ctx.last_code_authoring_repair_context is not None
        assert (
            ctx.last_code_authoring_repair_context.required_block_structure == "separated_browser_spine_plus_extraction"
        )

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

    def test_granted_advisory_arms_run_evidence_without_preflight_consume_when_run_attemptable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _spine_actuation_ctx()
        signature = "sig_preflight_dispatch"
        ctx.output_contract_actuation_by_signature[signature] = OutputContractAdvisoryState.GRANTED
        evaluation = workflow_update_module._OutputContractEvaluation(
            block_label="extract_record",
            artifact_id="art-1",
            required_paths={"output.confirmation_number"},
            observation_paths={"output.confirmation_number"},
            declaration_paths=set(),
            source="requested_output_contract",
            reason_code="requested_output_contract_missing_output_coverage",
            missing_metadata_paths=[],
            missing_schema_paths=[],
            missing_return_paths=[],
            shape_violations=["separated_spine_shape_violation"],
            canonical_signature=signature,
            payload={
                "reason_code": "requested_output_contract_missing_output_coverage",
                "missing_requested_output_facts": [],
            },
            repair_context=None,
            can_attempt_run=True,
        )
        monkeypatch.setattr(
            workflow_update_module,
            "_impose_output_contract_envelope_after_steering",
            lambda ctx, wf, meta: (wf, meta, False),
        )
        monkeypatch.setattr(
            workflow_update_module,
            "_scaffold_metadata_contract_for_update",
            lambda ctx, wf, meta: (meta, False),
        )
        monkeypatch.setattr(
            workflow_update_module,
            "_evaluate_output_contract_for_code_block",
            lambda *a, **k: evaluation,
        )

        result = workflow_update_module._metadata_contract_run_preflight_reject(ctx, "title: x", [])

        assert result is None
        assert ctx.output_contract_pending_run_evidence.get(signature) == ["output.confirmation_number"]
        assert ctx.output_contract_actuation_by_signature[signature] == OutputContractAdvisoryState.GRANTED

        run_result = {
            "data": {"blocks": [{"label": "extract_record", "extracted_data": {"confirmation_number": "R-9"}}]}
        }
        workflow_update_module.record_output_contract_run_output_evidence(ctx, run_result)
        assert ctx.output_contract_run_output_observed_by_signature[signature] is True
        assert ctx.output_contract_run_bound_required_path_by_signature[signature] is True


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

    def test_preflight_reject_persists_owner_ambiguity_repair_context(self) -> None:
        ctx = _spine_actuation_ctx()
        workflow_yaml = _dual_output_owner_yaml()

        result = workflow_update_module._metadata_contract_run_preflight_reject(ctx, workflow_yaml, [])

        assert result is not None
        assert result["ok"] is False
        assert ctx.last_code_authoring_repair_context is not None
        assert ctx.last_code_authoring_repair_context.reason_code == "output_owner_ambiguous"

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

    def test_split_imposed_yaml_clears_output_contract_run_gate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(workflow_update_module, "synthesize_code_block", lambda *a, **k: _fake_spine_synthesized())
        ctx = _spine_actuation_ctx()
        workflow_yaml = _collapsed_spine_yaml(
            _SPINE_SYNTH_CODE
            + '\nvalue = await page.locator("#result").inner_text()\nreturn {"output": {"record_id": value}}'
        )

        split_yaml, split_metadata, applied = workflow_update_module._impose_output_contract_envelope_after_steering(
            ctx, workflow_yaml, []
        )
        assert applied is True

        evaluation = workflow_update_module._evaluate_output_contract_for_code_block(
            ctx, split_yaml, split_metadata, allow_static_return_advisory=True
        )
        assert evaluation is not None
        assert workflow_update_module._SEPARATED_SPINE_SHAPE_REQUIRED_REASON_CODE not in evaluation.shape_violations
        assert evaluation.can_attempt_run or not evaluation.has_deficiencies

        assert workflow_update_module._metadata_contract_run_preflight_reject(ctx, split_yaml, split_metadata) is None

    def test_directive_satisfying_reauthor_passes_preflight(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(workflow_update_module, "synthesize_code_block", lambda *a, **k: _fake_spine_synthesized())
        ctx = _spine_actuation_ctx()
        collapsed_yaml = _collapsed_spine_yaml(
            "_setup = 1\n" + _SPINE_SYNTH_CODE + '\nreturn {"output": {"record_id": "X"}}'
        )

        _yaml_out, _meta, armed = workflow_update_module._impose_output_contract_envelope_after_steering(
            ctx, collapsed_yaml, []
        )
        assert armed is False
        assert ctx.output_contract_spine_directive_blockers_by_attempt_key

        schema = (
            '{"type":"object","properties":{"output":{"type":"object","properties":{"record_id":{"type":"string"}}}}}'
        )
        reauthored_yaml = _already_split_spine_yaml(base="extract_record")
        metadata = [
            {
                "block_label": "extract_record",
                "claimed_outcomes": [{"goal_value_paths": ["output.record_id"], "extraction_schema": schema}],
                "terminal_verifier_expectations": [
                    {"goal_value_paths": ["output.record_id"], "extraction_schema": schema}
                ],
            }
        ]

        assert workflow_update_module._metadata_contract_run_preflight_reject(ctx, reauthored_yaml, metadata) is None

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
        required_paths, source, reason_code = contract.union, contract.source, contract.reason_code
        read_label, _owner_labels = workflow_update_module._target_output_contract_block_label(
            ctx, workflow_yaml, scaffolded_metadata, required_paths
        )
        read_signature = workflow_update_module._output_contract_signature(
            ctx=ctx,
            workflow_yaml=workflow_yaml,
            source=source,
            reason_code=reason_code,
            required_paths=required_paths,
        )
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


class TestOutputContractBudgetRunGuard:
    def _prime(self, monkeypatch: pytest.MonkeyPatch) -> tuple[CopilotContext, _OutputContractEvaluation]:
        monkeypatch.setattr(workflow_update_module, "synthesize_code_block", lambda *a, **k: _fake_spine_synthesized())
        ctx = _spine_actuation_ctx()
        signature = workflow_update_module._stable_output_contract_key("turn:t-spine", {"output.record_id"})
        ctx.output_contract_reject_count_by_signature = {
            signature: workflow_update_module._MAX_OUTPUT_CONTRACT_REJECTS - 1
        }
        evaluation = _budget_evaluation(ctx)
        return ctx, evaluation

    def test_budget_rewrite_deferred_without_run_evidence(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx, evaluation = self._prime(monkeypatch)

        payload = workflow_update_module._record_output_contract_reject(ctx, evaluation, summary="x")

        assert payload["output_contract_reject_count"] >= workflow_update_module._MAX_OUTPUT_CONTRACT_REJECTS
        assert payload["reason_code"] == "output_contract_required"

    def test_budget_rewrite_deferred_for_author_time_reject_outcome(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx, evaluation = self._prime(monkeypatch)
        ctx.latest_recorded_build_test_outcome = RecordedBuildTestOutcome(
            phase="author_time_reject", verdict="authoring_rejected", reason_code="metadata_reject"
        )

        payload = workflow_update_module._record_output_contract_reject(ctx, evaluation, summary="x")

        assert payload["reason_code"] == "output_contract_required"

    def test_budget_rewrite_fires_with_persisted_run_outcome(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx, evaluation = self._prime(monkeypatch)
        ctx.latest_recorded_build_test_outcome = RecordedBuildTestOutcome(
            phase="persisted_block_run",
            verdict="repairable_failure",
            reason_code="failed_run",
            workflow_run_id="wr_1",
            structural_failure_identity="runtime:failed",
        )
        ctx.recorded_persisted_block_run_workflow_run_id = "wr_1"

        payload = workflow_update_module._record_output_contract_reject(ctx, evaluation, summary="x")

        assert payload["reason_code"] == "output_contract_reject_budget_exhausted"

    def _prime_at_reject_count(
        self, monkeypatch: pytest.MonkeyPatch, *, reject_count: int, deferral_count: int
    ) -> tuple[CopilotContext, _OutputContractEvaluation]:
        monkeypatch.setattr(workflow_update_module, "synthesize_code_block", lambda *a, **k: _fake_spine_synthesized())
        ctx = _spine_actuation_ctx()
        signature = workflow_update_module._stable_output_contract_key("turn:t-spine", {"output.record_id"})
        ctx.output_contract_reject_count_by_signature = {signature: reject_count}
        ctx.output_contract_deferral_count_by_signature = {signature: deferral_count}
        evaluation = _budget_evaluation(ctx)
        return ctx, evaluation

    def test_zero_run_reject_loop_terminalizes_at_deferral_cap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx, evaluation = self._prime_at_reject_count(
            monkeypatch,
            reject_count=workflow_update_module._MAX_OUTPUT_CONTRACT_REJECTS,
            deferral_count=workflow_update_module._MAX_OUTPUT_CONTRACT_DEFERRALS - 1,
        )

        payload = workflow_update_module._record_output_contract_reject(ctx, evaluation, summary="x")

        assert payload["reason_code"] == "output_contract_reject_budget_exhausted"

    def test_zero_run_reject_below_deferral_cap_still_defers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx, evaluation = self._prime_at_reject_count(
            monkeypatch,
            reject_count=workflow_update_module._MAX_OUTPUT_CONTRACT_REJECTS,
            deferral_count=0,
        )

        payload = workflow_update_module._record_output_contract_reject(ctx, evaluation, summary="x")

        assert payload["reason_code"] == "output_contract_required"

    def test_deferral_cap_matches_max_rejects_plus_two(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(workflow_update_module, "synthesize_code_block", lambda *a, **k: _fake_spine_synthesized())
        ctx = _spine_actuation_ctx()
        signature = workflow_update_module._stable_output_contract_key("turn:t-spine", {"output.record_id"})
        ctx.output_contract_reject_count_by_signature = {
            signature: workflow_update_module._MAX_OUTPUT_CONTRACT_REJECTS - 1
        }
        ctx.output_contract_deferral_count_by_signature = {}

        reasons: list[str] = []
        for _ in range(6):
            evaluation = _budget_evaluation(ctx)
            payload = workflow_update_module._record_output_contract_reject(ctx, evaluation, summary="x")
            reasons.append(str(payload["reason_code"]))
            if payload["reason_code"] == "output_contract_reject_budget_exhausted":
                break

        assert reasons[-1] == "output_contract_reject_budget_exhausted"
        assert reasons[:-1] == ["output_contract_required"] * (len(reasons) - 1)
        assert (
            ctx.output_contract_reject_count_by_signature[signature]
            == workflow_update_module._MAX_OUTPUT_CONTRACT_REJECTS + 2
        )


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

    @pytest.mark.asyncio
    async def test_undeclared_parameter_key_rejects_before_persist(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        ctx.workflow_yaml = _SAFE_CODE_YAML
        ctx.last_code_authoring_repair_context = _stale_unresolved_repair_context()
        submitted = _directory_blocks_yaml(
            """
            - block_type: code
              label: search_directory
              parameter_keys: [address_or_postal_code]
              code: |
                await page.locator("#location").fill(str(address_or_postal_code))
            """
        )

        result = await _update_workflow({"workflow_yaml": submitted}, ctx)

        assert result["ok"] is False
        assert "search_directory" in result["error"]
        assert "`address_or_postal_code`" in result["error"]
        assert "workflow_definition.parameters" in result["error"]
        assert ctx.workflow_yaml == _SAFE_CODE_YAML
        assert "authoring_repair_context" not in result["data"]
        assert ctx.last_code_authoring_repair_context is None

    @pytest.mark.asyncio
    async def test_nested_code_block_undeclared_parameter_key_rejects(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        submitted = _directory_blocks_yaml(
            """
            - block_type: loop
              label: retry_search
              loop_blocks:
              - block_type: code
                label: nested_directory_search
                parameter_keys: [address_or_postal_code]
                code: |
                  await page.locator("#location").fill(str(address_or_postal_code))
            """
        )

        result = await _update_workflow({"workflow_yaml": submitted}, ctx)

        assert result["ok"] is False
        assert "nested_directory_search" in result["error"]
        assert "`address_or_postal_code`" in result["error"]
        assert ctx.workflow_yaml == ""

    def test_deeply_nested_parameter_contract_returns_validation_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        block: dict[str, object] = {
            "block_type": "code",
            "label": "search_directory",
            "parameter_keys": ["search_location"],
            "code": "print(search_location)",
        }
        for index in range(1100):
            block = {
                "block_type": "loop",
                "label": f"loop_{index}",
                "loop_blocks": [block],
            }
        parsed = {
            "workflow_definition": {
                "parameters": [{"key": "search_location"}],
                "blocks": [block],
            }
        }
        monkeypatch.setattr(workflow_update_module, "parse_workflow_yaml", lambda _workflow_yaml: parsed)

        assert (
            workflow_update_module._code_block_parameter_contract_error("workflow_definition: {}")
            == "Workflow YAML nesting is too deep to validate."
        )

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

    @pytest.mark.parametrize(
        ("submitted", "label", "key"),
        [
            (
                _directory_blocks_yaml(
                    """
                    - block_type: code
                      label: summarize_registry
                      parameter_keys: [search_registry_output]
                      code: |
                        print(search_registry_output)
                    - block_type: code
                      label: search_registry
                      code: |
                        return {"records": []}
                    """
                ),
                "summarize_registry",
                "search_registry_output",
            ),
            (
                _directory_blocks_yaml(
                    """
                    - block_type: loop
                      label: retry_search
                      loop_blocks:
                      - block_type: code
                        label: nested_directory_search
                        parameter_keys: [retry_search_output]
                        code: |
                          print(retry_search_output)
                    """
                ),
                "nested_directory_search",
                "retry_search_output",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_unavailable_output_parameter_key_rejects(
        self, monkeypatch: pytest.MonkeyPatch, submitted: str, label: str, key: str
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()

        result = await _update_workflow({"workflow_yaml": submitted}, ctx)

        assert result["ok"] is False
        assert label in result["error"]
        assert f"`{key}`" in result["error"]
        assert ctx.workflow_yaml == ""

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

    @pytest.mark.asyncio
    async def test_conflict_marker_inside_code_block_has_marker_specific_error(
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
                label: search_registry
                code: |
                  <<<<<<< HEAD
                  await page.goto("https://example.com/search")
            """
        )

        result = await _update_workflow({"workflow_yaml": submitted}, ctx)

        assert result["ok"] is False
        assert "Code block `search_registry` contains unresolved conflict marker `<<<<<<< HEAD`" in result["error"]
        assert "not valid Python" not in result["error"]
        assert ctx.workflow_yaml == ""

    @pytest.mark.asyncio
    async def test_registered_download_output_keys_are_not_parameter_keys(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        submitted = _yaml(
            """
            title: Statement download
            workflow_definition:
              blocks:
              - block_type: code
                label: download_statement
                parameter_keys: [downloaded_files]
                code: |
                  print(downloaded_files)
            """
        )

        result = await _update_workflow({"workflow_yaml": submitted}, ctx)

        assert result["ok"] is False
        assert "download_statement" in result["error"]
        assert "`downloaded_files`" in result["error"]
        assert "execution layer injects registered download output keys" in result["error"]


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
    async def test_imposition_appends_no_fill_skeleton_and_emits_no_skeleton_substitution(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = self._provider_search_ctx()
        metadata = [_terminal_metadata("search_registry", "search the registry")]

        result = await _update_workflow(
            {"workflow_yaml": _SUBMITTED_LITERAL_YAML, "code_artifact_metadata": metadata}, ctx
        )

        assert result["ok"] is True
        substitutions = result["data"]["imposed_substitutions"]
        assert substitutions["scrubbed_stale_selected_goal_value_paths"] is True
        assert "preserved_submitted_extraction_code" not in substitutions
        parsed = parse_workflow_yaml(ctx.workflow_yaml)
        assert isinstance(parsed, dict)
        block = _single_code_block(parsed)
        assert 'await page.locator("#provInput").fill(str(provider_name))' in block["code"]
        assert not workflow_update_module._artifact_declares_goal_values(ctx.code_artifact_metadata["search_registry"])

    @pytest.mark.asyncio
    async def test_output_intent_requires_artifact_metadata_before_persist(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = self._provider_search_ctx()
        ctx.last_code_authoring_repair_context = _stale_unresolved_repair_context()
        submitted = _yaml(
            """
            title: Provider lookup
            workflow_definition:
              parameters:
              - {parameter_type: output, key: provider_result}
              blocks:
              - block_type: code
                label: search_registry
                prompt: Search the registry and return structured provider result data.
                code: |
                  await page.locator("input[placeholder='Search']").fill("Sample Search")
                  await page.locator("button.lookup").click()
            """
        )

        result = await _update_workflow({"workflow_yaml": submitted}, ctx)

        assert result["ok"] is False
        assert "must pass `code_artifact_metadata`" in result["error"]
        assert "goal_value_paths" in result["error"]
        assert ctx.workflow_yaml == ""
        assert "authoring_repair_context" not in result["data"]
        assert ctx.last_code_authoring_repair_context is None
        assert ctx.latest_recorded_build_test_outcome is not None
        assert ctx.latest_recorded_build_test_outcome.reason_code == "metadata_reject"
        assert ctx.latest_recorded_build_test_outcome.is_authoritative is True
        assert ctx.code_authoring_guardrail_reject_count == 1
        assert ctx.last_code_authoring_reject_was_credential_priority is False

    @pytest.mark.asyncio
    async def test_missing_metadata_with_credential_gap_uses_credential_priority_counter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        ctx.scout_trajectory = []
        submitted = _yaml(
            """
            title: Credentialed lookup
            workflow_definition:
              parameters:
              - {parameter_type: workflow, workflow_parameter_type: credential_id, key: login_credential, default_value: cred_missing}
              - {parameter_type: output, key: lookup_result}
              blocks:
              - block_type: code
                label: lookup_with_saved_credential
                parameter_keys: [login_credential]
                prompt: Sign in and return the lookup result.
                code: |
                  await page.locator("#email").fill(login_credential.username)
                  await page.locator("input[type='password']").fill(login_credential.password)
                  return {"lookup_result": "sample"}
            """
        )

        result = await _update_workflow({"workflow_yaml": submitted}, ctx)

        assert result["ok"] is False
        assert "must pass `code_artifact_metadata`" in result["error"]
        assert ctx.code_authoring_guardrail_reject_count == 1
        assert ctx.last_code_authoring_reject_was_credential_priority is True
        assert ctx.blocker_signal is None

    @pytest.mark.asyncio
    async def test_output_intent_rejects_partial_metadata_without_output_label(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = self._provider_search_ctx()
        submitted = _yaml(
            """
            title: Provider lookup
            workflow_definition:
              parameters:
              - {parameter_type: output, key: provider_result}
              blocks:
              - block_type: code
                label: open_registry
                prompt: Open the registry.
                code: |
                  await page.goto("https://example.com/search")
              - block_type: code
                label: extract_registry
                prompt: Return structured provider result data.
                code: |
                  records = [{"number": "REC-001"}]
            """
        )

        result = await _update_workflow(
            {
                "workflow_yaml": submitted,
                "code_artifact_metadata": [_terminal_metadata("open_registry", "open the registry")],
            },
            ctx,
        )

        assert result["ok"] is False
        assert "extract_registry" in result["error"]
        assert "must pass `code_artifact_metadata`" in result["error"]
        assert ctx.workflow_yaml == ""

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
    async def test_invalid_metadata_records_typed_outcome_without_prose_payload(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        submitted = _yaml(
            """
            title: Provider lookup
            workflow_definition:
              blocks:
              - block_type: code
                label: search_registry
                code: |
                  records = [{"number": "REC-001"}]
            """
        )
        metadata = [
            {
                "block_label": "search_registry",
                "claimed_outcomes": [{"id": "claim:rows", "status": "satisfied"}],
            }
        ]

        result = await _update_workflow({"workflow_yaml": submitted, "code_artifact_metadata": metadata}, ctx)

        assert result["ok"] is False
        assert "Artifact metadata" in result["error"]
        assert ctx.latest_recorded_build_test_outcome is not None
        assert ctx.latest_recorded_build_test_outcome.reason_code == "metadata_reject"
        assert ctx.latest_recorded_build_test_outcome.is_authoritative is True
        structural_payload = workflow_update_module._code_artifact_metadata_reject_payload(
            workflow_yaml=submitted,
            raw_metadata=metadata,
            offending_labels=["search_registry"],
            violation_categories=["missing_declared_goal", "missing_required_list", "missing_artifact_refs"],
        )
        assert structural_payload is not None
        assert structural_payload["offending_labels"] == ["search_registry"]
        assert structural_payload["missing_fields_by_label"] == {
            "search_registry": [
                "declared_goal",
                "page_dependencies",
                "completion_criteria",
                "terminal_verifier_expectations",
                "evidence_refs_or_observation_refs",
            ]
        }
        assert structural_payload["code_block_output_status"] == {
            "search_registry": {
                "block_type": "code",
                "has_code": True,
                "declares_output_intent": False,
                "declares_output_roots": [],
                "has_meaningful_output": False,
            }
        }
        payload_text = str(structural_payload)
        assert "requires non-empty" not in payload_text
        assert "claim:rows" not in payload_text

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
    async def test_imposition_preserves_custom_extraction_code_with_goal_metadata(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = self._provider_search_ctx()
        synthesized = workflow_update_module.synthesize_code_block(ctx.scout_trajectory, strict_selectors=True)
        assert synthesized is not None
        synthesized_code = textwrap.dedent(synthesized.code).lstrip()
        submitted_code = (
            "records = []\n"
            + synthesized_code.rstrip()
            + '\nrecords.append({"number": "REC-001", "status": "credentialed"})\n'
        )
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

        assert result["ok"] is False
        assert "selected output extraction boundary is ambiguous" in result["error"]

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

    @pytest.mark.asyncio
    async def test_imposition_carries_reached_download_target_to_synthesized_code(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _code_only_ctx()
        _enable_imposition(ctx)
        ctx.scout_trajectory = [
            {
                "tool_name": "click",
                "selector": "div.stmt-row",
                "source_url": "https://example.com/bills",
                "trajectory_index": 0,
            }
        ]
        ctx.reached_download_target = ReachedDownloadTarget(
            selector='[href="/files/report.pdf"]',
            affordance_text="Download PDF",
            download_kind="extension",
            source_step="trajectory_recency",
            already_registered=False,
        )
        workflow_yaml = _yaml(
            """
            title: Download report
            workflow_definition:
              blocks:
              - block_type: code
                label: download_report
                code: |
                  await page.goto("https://example.com/bills")
                  await page.locator("div.stmt-row").click()
            """
        )

        result = workflow_update_module._maybe_impose_synthesized_code_block(workflow_yaml, ctx)

        assert result.violations == []
        parsed = parse_workflow_yaml(result.workflow_yaml)
        assert isinstance(parsed, dict)
        block = _single_code_block(parsed)
        assert "expect_download" in block["code"]
        assert 'await page.locator("[href=\\"/files/report.pdf\\"]").click()' in block["code"]

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
    async def test_mixed_locator_fill_rejects_direct_synthesized_parameter_reference(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = self._provider_search_ctx()
        ctx.last_code_authoring_repair_context = _stale_unresolved_repair_context()

        result = await _update_workflow({"workflow_yaml": _SUBMITTED_MIXED_LOCATOR_FILL_COMPUTED_LITERAL_YAML}, ctx)

        assert result["ok"] is False
        assert "submitted code mixes direct fills using `provider_name`" in result["error"]
        assert ctx.workflow_yaml == ""
        repair_context = result["data"]["authoring_repair_context"]
        assert repair_context["reason_code"] == "synthesized_parameter_binding_ambiguous"
        assert repair_context["unresolved_names"] == ["provider_name"]
        assert isinstance(ctx.last_code_authoring_repair_context, CodeAuthoringRepairContext)
        assert ctx.last_code_authoring_repair_context.model_dump(mode="json") == repair_context

    @pytest.mark.asyncio
    async def test_typed_default_contract_reject_clears_stale_repair_context(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = self._typed_default_ctx()
        submitted = _yaml(
            """
            title: Product lookup
            workflow_definition:
              parameters: invalid
              blocks:
              - block_type: code
                label: search_catalog
                code: |
                  await page.locator("#search").fill("example_sku_123")
            """
        )
        ctx.workflow_yaml = submitted
        ctx.last_code_authoring_repair_context = _stale_unresolved_repair_context()

        result = await _update_workflow({"workflow_yaml": submitted}, ctx)

        assert result["ok"] is False
        assert "workflow_definition.parameters must be a list" in result["error"]
        assert "authoring_repair_context" not in result["data"]
        assert ctx.last_code_authoring_repair_context is None

    @pytest.mark.asyncio
    async def test_unknown_computed_parameter_rejects_before_persist(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_successful_update(monkeypatch)
        ctx = self._provider_search_ctx()

        result = await _update_workflow({"workflow_yaml": _SUBMITTED_UNKNOWN_COMPUTED_LITERAL_YAML}, ctx)

        assert result["ok"] is False
        assert "Unable to bind synthesized parameter `provider_name`" in result["error"]
        repair_context = result["data"]["authoring_repair_context"]
        assert repair_context["reason_code"] == "synthesized_parameter_binding_ambiguous"
        assert repair_context["unresolved_names"] == ["provider_name"]
        assert isinstance(ctx.last_code_authoring_repair_context, CodeAuthoringRepairContext)
        assert ctx.last_code_authoring_repair_context.model_dump(mode="json") == repair_context
        assert ctx.latest_recorded_build_test_outcome is not None
        assert ctx.latest_recorded_build_test_outcome.reason_code == "synthesized_parameter_binding_ambiguous"
        assert ctx.latest_recorded_build_test_outcome.is_authoritative is True
        assert ctx.workflow_yaml == ""

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
    async def test_single_synthesized_key_without_bindable_fill_records_repair_context(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        _enable_imposition(ctx)
        ctx.scout_trajectory = [
            {
                "tool_name": "type_text",
                "selector": "#location",
                "source_url": "https://example.com/directory",
                "typed_length": 5,
                "role": "textbox",
                "accessible_name": "Address City County or Zip Code",
                "trajectory_index": 0,
            }
        ]
        submitted = _yaml(
            """
            title: Directory lookup
            workflow_definition:
              blocks:
              - block_type: code
                label: search_directory
                code: |
                  await page.locator("#submit").click()
            """
        )

        result = await _update_workflow({"workflow_yaml": submitted}, ctx)

        assert result["ok"] is False
        assert "Unable to bind synthesized parameter `address_city_county_or_zip_code`" in result["error"]
        repair_context = result["data"]["authoring_repair_context"]
        assert repair_context["reason_code"] == "synthesized_parameter_binding_ambiguous"
        assert repair_context["unresolved_names"] == ["address_city_county_or_zip_code"]
        assert repair_context["selector"] == "#location"
        assert repair_context["source_url"] == "https://example.com"
        assert isinstance(ctx.last_code_authoring_repair_context, CodeAuthoringRepairContext)
        assert ctx.latest_recorded_build_test_outcome is not None
        assert ctx.latest_recorded_build_test_outcome.reason_code == "synthesized_parameter_binding_ambiguous"
        assert ctx.latest_recorded_build_test_outcome.is_authoritative is True
        assert ctx.workflow_yaml == ""

    @pytest.mark.asyncio
    async def test_parameter_binding_after_two_steering_cycles_imposes_synthesized_binding(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = self._provider_search_ctx()
        first = await _update_workflow({"workflow_yaml": _SUBMITTED_UNKNOWN_COMPUTED_LITERAL_YAML}, ctx)
        second = await _update_workflow({"workflow_yaml": _SUBMITTED_UNKNOWN_COMPUTED_LITERAL_YAML}, ctx)
        imposed = await _update_workflow({"workflow_yaml": _SUBMITTED_UNKNOWN_COMPUTED_LITERAL_YAML}, ctx)

        assert first["ok"] is False
        assert second["ok"] is False
        assert imposed["ok"] is True
        parsed = parse_workflow_yaml(ctx.workflow_yaml)
        assert isinstance(parsed, dict)
        block = _single_code_block(parsed)
        assert block["parameter_keys"] == ["provider_name"]
        assert 'await page.locator("#provInput").fill(str(provider_name))' in block["code"]

    @pytest.mark.asyncio
    async def test_parameter_binding_after_two_steering_cycles_imposes_for_mixed_fill(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = self._provider_search_ctx()

        first = await _update_workflow({"workflow_yaml": _SUBMITTED_MIXED_LOCATOR_FILL_COMPUTED_LITERAL_YAML}, ctx)
        second = await _update_workflow({"workflow_yaml": _SUBMITTED_MIXED_LOCATOR_FILL_COMPUTED_LITERAL_YAML}, ctx)
        imposed = await _update_workflow({"workflow_yaml": _SUBMITTED_MIXED_LOCATOR_FILL_COMPUTED_LITERAL_YAML}, ctx)

        assert first["ok"] is False
        assert second["ok"] is False
        assert imposed["ok"] is True
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

    def test_parameter_binding_uses_canonical_output_contract_budget(self) -> None:
        ctx = self._provider_search_ctx()
        ctx.turn_id = "parameter-binding-contract-budget"
        ctx.request_policy = RequestPolicy(
            completion_criteria=[
                SimpleNamespace(
                    id="requested_value",
                    output_path="output.record_id",
                    level="run",
                    method_mandated=False,
                    kind="outcome",
                )
            ]
        )
        signature = workflow_update_module._output_contract_signature(
            ctx=ctx,
            workflow_yaml="title: First\nworkflow_definition:\n  blocks: []\n",
            source="metadata_reject",
            reason_code="metadata_reject",
            required_paths={"output.record_id"},
        )
        ctx.output_contract_reject_count_by_signature = {signature: 2}
        parsed = parse_workflow_yaml(_SUBMITTED_UNKNOWN_COMPUTED_LITERAL_YAML)
        assert isinstance(parsed, dict)
        block = _single_code_block(parsed)
        synthesized = workflow_update_module.synthesize_code_block(ctx.scout_trajectory, strict_selectors=True)
        assert synthesized is not None

        reconciliation = workflow_update_module._reconcile_synthesized_parameters(
            ctx=ctx,
            parsed=parsed,
            code_block=block,
            submitted_code=str(block.get("code") or ""),
            synthesized_parameters=synthesized.parameters,
            scout_trajectory=ctx.scout_trajectory,
        )

        assert reconciliation.violations == []
        assert reconciliation.parameter_keys == ["provider_name"]
        assert parsed["workflow_definition"]["parameters"] == [
            {
                "parameter_type": "workflow",
                "workflow_parameter_type": "string",
                "key": "provider_name",
            }
        ]

    @pytest.mark.asyncio
    async def test_synthesized_parameter_repair_context_uses_safe_selector_atom(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        _enable_imposition(ctx)
        raw_selector = "#location-" + ("x" * 180)
        ctx.scout_trajectory = [
            {
                "tool_name": "type_text",
                "selector": raw_selector,
                "source_url": "https://example.com/directory?query=raw",
                "typed_length": 5,
                "role": "textbox",
                "accessible_name": "Address City County or Zip Code",
                "trajectory_index": 0,
            }
        ]
        submitted = _yaml(
            """
            title: Directory lookup
            workflow_definition:
              blocks:
              - block_type: code
                label: search_directory
                code: |
                  await page.locator("#submit").click()
            """
        )

        result = await _update_workflow({"workflow_yaml": submitted}, ctx)

        assert result["ok"] is False
        repair_context = result["data"]["authoring_repair_context"]
        assert repair_context["reason_code"] == "synthesized_parameter_binding_ambiguous"
        assert repair_context["unresolved_names"] == ["address_city_county_or_zip_code"]
        assert repair_context["selector"] in (None, "")
        assert repair_context["source_url"] == "https://example.com"
        assert raw_selector not in str(repair_context)

    @pytest.mark.asyncio
    async def test_multi_input_missing_synthesized_parameter_rejects_with_repair_context(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        _enable_imposition(ctx)
        ctx.scout_trajectory = [
            {
                "tool_name": "type_text",
                "selector": "#confirmation",
                "source_url": "https://example.com/orders",
                "typed_length": 10,
                "role": "textbox",
                "accessible_name": "Enter Confirmation",
                "trajectory_index": 0,
            },
            {
                "tool_name": "type_text",
                "selector": "#postal",
                "source_url": "https://example.com/orders",
                "typed_length": 5,
                "role": "textbox",
                "accessible_name": "ZIP Code",
                "trajectory_index": 1,
            },
        ]
        submitted = _yaml(
            """
            title: Order lookup
            workflow_definition:
              parameters:
              - {parameter_type: workflow, workflow_parameter_type: string, key: order_lookup}
              blocks:
              - block_type: code
                label: order_status
                parameter_keys: [order_lookup]
                code: |
                  await page.locator("#confirmation").fill(str(order_lookup))
                  await page.locator("#postal").fill(str(order_lookup))
            """
        )

        result = await _update_workflow({"workflow_yaml": submitted}, ctx)

        assert result["ok"] is False
        assert "authored selector-join alias is reused by another synthesized input" in result["error"]
        assert ctx.workflow_yaml == ""
        repair_context = result["data"]["authoring_repair_context"]
        assert repair_context["reason_code"] == "synthesized_parameter_binding_ambiguous"
        assert repair_context["block_label"] == "order_status"
        assert repair_context["unresolved_names"] == ["zip_code"]
        assert repair_context["available_parameter_keys"] == ["order_lookup"]
        assert repair_context["binding_candidates"] == ["zip_code", "order_lookup"]
        assert "update_and_run_blocks" in repair_context["repair_instruction"]
        assert "secret" not in str(repair_context).lower()
        assert isinstance(ctx.last_code_authoring_repair_context, CodeAuthoringRepairContext)
        assert ctx.last_code_authoring_repair_context.model_dump(mode="json") == repair_context

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

    @pytest.mark.asyncio
    async def test_synthesized_parameter_does_not_alias_by_typed_length_to_declared_default(
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
                "role": "textbox",
                "accessible_name": "Address or postal code",
                "trajectory_index": 0,
            },
            {
                "tool_name": "type_text",
                "selector": "#firstName",
                "source_url": "https://example.com/find-care",
                "typed_length": 5,
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

        assert result["ok"] is False
        assert "Unable to bind synthesized parameter `address_or_postal_code`" in result["error"]
        repair_context = result["data"]["authoring_repair_context"]
        assert repair_context["reason_code"] == "synthesized_parameter_binding_ambiguous"
        assert repair_context["unresolved_names"] == ["address_or_postal_code"]
        assert ctx.workflow_yaml == ""
        assert isinstance(ctx.last_code_authoring_repair_context, CodeAuthoringRepairContext)
        assert ctx.latest_recorded_build_test_outcome is not None
        assert ctx.latest_recorded_build_test_outcome.reason_code == "synthesized_parameter_binding_ambiguous"
        assert ctx.latest_recorded_build_test_outcome.is_authoritative is True

    @pytest.mark.asyncio
    async def test_synthesized_provider_search_key_does_not_compose_declared_first_last_inputs(
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
                "selector": "#providerSearch",
                "source_url": "https://example.com/find-care",
                "typed_length": 12,
                "role": "textbox",
                "accessible_name": "Provider name or identifier",
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
              - {key: provider_last_name, default_value: "Family"}
              blocks:
              - block_type: code
                label: search_directory
                parameter_keys:
                - address_or_postal_code
                - provider_name_or_identifier
                code: |
                  await page.locator("#location").fill(str(address_or_postal_code))
                  await page.locator("#providerSearch").fill(str(provider_name_or_identifier))
            """
        )

        result = await _update_workflow({"workflow_yaml": submitted}, ctx)

        assert result["ok"] is False
        assert "Unable to bind synthesized parameter `provider_name_or_identifier`" in result["error"]
        repair_context = result["data"]["authoring_repair_context"]
        assert repair_context["reason_code"] == "synthesized_parameter_binding_ambiguous"
        assert repair_context["unresolved_names"] == ["provider_name_or_identifier"]
        assert ctx.workflow_yaml == ""
        assert isinstance(ctx.last_code_authoring_repair_context, CodeAuthoringRepairContext)
        assert ctx.latest_recorded_build_test_outcome is not None
        assert ctx.latest_recorded_build_test_outcome.reason_code == "synthesized_parameter_binding_ambiguous"
        assert ctx.latest_recorded_build_test_outcome.is_authoritative is True

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
    async def test_single_synthesized_location_key_rejects_unique_declared_search_location_without_exact_default(
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
                "typed_length": 19,
                "role": "textbox",
                "accessible_name": "Address City County or Zip Code",
                "trajectory_index": 0,
            }
        ]
        submitted = _yaml(
            """
            title: Directory lookup
            workflow_definition:
              parameters:
              - {key: plan, default_value: "Coastal Complete Health"}
              - {key: provider_first_name, default_value: "Jordan"}
              - {key: provider_last_name, default_value: "Avery"}
              - {key: provider_npi, default_value: "1457803926"}
              - {key: search_location, default_value: "North Carolina, USA"}
              blocks:
              - block_type: code
                label: open_directory_plan_selection
                parameter_keys:
                - address_city_county_or_zip_code
                code: |
                  await page.locator("#planContinue").click()
            """
        )

        result = await _update_workflow({"workflow_yaml": submitted}, ctx)

        assert result["ok"] is False
        assert "Unable to bind synthesized parameter `address_city_county_or_zip_code`" in result["error"]
        repair_context = result["data"]["authoring_repair_context"]
        assert repair_context["reason_code"] == "synthesized_parameter_binding_ambiguous"
        assert repair_context["unresolved_names"] == ["address_city_county_or_zip_code"]
        assert repair_context["available_parameter_keys"] == [
            "plan",
            "provider_first_name",
            "provider_last_name",
            "provider_npi",
            "search_location",
        ]
        assert ctx.workflow_yaml == ""
        assert isinstance(ctx.last_code_authoring_repair_context, CodeAuthoringRepairContext)
        assert ctx.latest_recorded_build_test_outcome is not None
        assert ctx.latest_recorded_build_test_outcome.reason_code == "synthesized_parameter_binding_ambiguous"
        assert ctx.latest_recorded_build_test_outcome.is_authoritative is True

    @pytest.mark.asyncio
    async def test_single_synthesized_location_key_rejects_unique_declared_search_location_without_default(
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
                "typed_length": 19,
                "role": "textbox",
                "accessible_name": "Address City County or Zip Code",
                "trajectory_index": 0,
            }
        ]
        submitted = _yaml(
            """
            title: Directory lookup
            workflow_definition:
              parameters:
              - {parameter_type: workflow, workflow_parameter_type: string, key: plan}
              - {parameter_type: workflow, workflow_parameter_type: string, key: provider_first_name}
              - {parameter_type: workflow, workflow_parameter_type: string, key: provider_last_name}
              - {parameter_type: workflow, workflow_parameter_type: string, key: provider_npi}
              - {parameter_type: workflow, workflow_parameter_type: string, key: search_location}
              blocks:
              - block_type: code
                label: open_directory_plan_selection
                parameter_keys:
                - address_city_county_or_zip_code
                code: |
                  await page.locator("#planContinue").click()
            """
        )

        result = await _update_workflow({"workflow_yaml": submitted}, ctx)

        assert result["ok"] is False
        assert "Unable to bind synthesized parameter `address_city_county_or_zip_code`" in result["error"]
        repair_context = result["data"]["authoring_repair_context"]
        assert repair_context["reason_code"] == "synthesized_parameter_binding_ambiguous"
        assert repair_context["unresolved_names"] == ["address_city_county_or_zip_code"]
        assert repair_context["available_parameter_keys"] == [
            "plan",
            "provider_first_name",
            "provider_last_name",
            "provider_npi",
            "search_location",
        ]
        assert ctx.workflow_yaml == ""
        assert isinstance(ctx.last_code_authoring_repair_context, CodeAuthoringRepairContext)
        assert ctx.latest_recorded_build_test_outcome is not None
        assert ctx.latest_recorded_build_test_outcome.reason_code == "synthesized_parameter_binding_ambiguous"
        assert ctx.latest_recorded_build_test_outcome.is_authoritative is True

    @pytest.mark.asyncio
    async def test_single_synthesized_location_key_rejects_ambiguous_declared_location_alias_without_defaults(
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
                "typed_length": 19,
                "role": "textbox",
                "accessible_name": "Address City County or Zip Code",
                "trajectory_index": 0,
            }
        ]
        submitted = _yaml(
            """
            title: Directory lookup
            workflow_definition:
              parameters:
              - {parameter_type: workflow, workflow_parameter_type: string, key: search_location}
              - {parameter_type: workflow, workflow_parameter_type: string, key: service_location}
              blocks:
              - block_type: code
                label: open_directory_plan_selection
                parameter_keys:
                - address_city_county_or_zip_code
                code: |
                  await page.locator("#planContinue").click()
            """
        )

        result = await _update_workflow({"workflow_yaml": submitted}, ctx)

        assert result["ok"] is False
        assert "Unable to bind synthesized parameter `address_city_county_or_zip_code`" in result["error"]
        repair_context = result["data"]["authoring_repair_context"]
        assert repair_context["reason_code"] == "synthesized_parameter_binding_ambiguous"
        assert repair_context["unresolved_names"] == ["address_city_county_or_zip_code"]
        assert repair_context["available_parameter_keys"] == ["search_location", "service_location"]
        assert ctx.latest_recorded_build_test_outcome is not None
        assert ctx.latest_recorded_build_test_outcome.is_authoritative is True

    @pytest.mark.asyncio
    async def test_single_synthesized_location_key_keeps_authoritative_reject_when_length_alias_ambiguous(
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
                "typed_length": 19,
                "role": "textbox",
                "accessible_name": "Address City County or Zip Code",
                "trajectory_index": 0,
            }
        ]
        submitted = _yaml(
            """
            title: Directory lookup
            workflow_definition:
              parameters:
              - {key: search_location, default_value: "North Carolina, USA"}
              - {key: alternate_location, default_value: "South Carolina, USA"}
              blocks:
              - block_type: code
                label: open_directory_plan_selection
                parameter_keys:
                - address_city_county_or_zip_code
                code: |
                  await page.locator("#planContinue").click()
            """
        )

        result = await _update_workflow({"workflow_yaml": submitted}, ctx)

        assert result["ok"] is False
        assert "Unable to bind synthesized parameter `address_city_county_or_zip_code`" in result["error"]
        repair_context = result["data"]["authoring_repair_context"]
        assert repair_context["reason_code"] == "synthesized_parameter_binding_ambiguous"
        assert repair_context["block_label"] == "open_directory_plan_selection"
        assert repair_context["unresolved_names"] == ["address_city_county_or_zip_code"]
        assert repair_context["available_parameter_keys"] == ["alternate_location", "search_location"]
        assert isinstance(ctx.last_code_authoring_repair_context, CodeAuthoringRepairContext)
        assert ctx.latest_recorded_build_test_outcome is not None
        assert ctx.latest_recorded_build_test_outcome.reason_code == "synthesized_parameter_binding_ambiguous"
        assert ctx.latest_recorded_build_test_outcome.is_authoritative is True

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
    async def test_single_submitted_string_parameter_is_not_adopted_by_length_for_synthesized_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = self._provider_search_ctx()
        ctx.scout_trajectory[0]["accessible_name"] = "Search by doctor name or specialty, hospital, procedure, and more"

        result = await _update_workflow({"workflow_yaml": _SUBMITTED_COMPUTED_PARAMETER_YAML}, ctx)

        synthesized_key = "search_by_doctor_name_or_specialty_hospital_procedure_and_more"
        assert result["ok"] is False
        assert f"Unable to bind synthesized parameter `{synthesized_key}`" in result["error"]
        repair_context = result["data"]["authoring_repair_context"]
        assert repair_context["reason_code"] == "synthesized_parameter_binding_ambiguous"
        assert repair_context["unresolved_names"] == [synthesized_key]
        assert ctx.workflow_yaml == ""
        assert isinstance(ctx.last_code_authoring_repair_context, CodeAuthoringRepairContext)
        assert ctx.latest_recorded_build_test_outcome is not None
        assert ctx.latest_recorded_build_test_outcome.reason_code == "synthesized_parameter_binding_ambiguous"
        assert ctx.latest_recorded_build_test_outcome.is_authoritative is True

    @pytest.mark.asyncio
    async def test_mixed_literal_and_computed_fill_rejects_before_persist(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = self._provider_search_ctx()

        result = await _update_workflow({"workflow_yaml": _SUBMITTED_MIXED_LITERAL_YAML}, ctx)

        assert result["ok"] is False
        assert "submitted code mixes direct fills using `provider_name`" in result["error"]
        assert ctx.workflow_yaml == ""

    @pytest.mark.asyncio
    async def test_promotes_scouted_typed_literal_across_multiple_and_nested_code_blocks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = self._typed_default_ctx()

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


def test_direct_parameter_binding_ignores_nested_function_assignment() -> None:
    code = textwrap.dedent(
        """
        def normalize_search() -> str:
            search = "local fallback"
            return search

        await page.locator("#search").fill(search)
        """
    ).strip()

    assert workflow_update_module._submitted_uses_parameter_in_direct_fill_type(code, "search") is True


def test_direct_parameter_binding_rejects_top_level_assignment() -> None:
    code = textwrap.dedent(
        """
        search = "local fallback"
        await page.locator("#search").fill(search)
        """
    ).strip()

    assert workflow_update_module._submitted_uses_parameter_in_direct_fill_type(code, "search") is False


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
    async def test_conforming_label_persists_when_sibling_label_rejects(self) -> None:
        workflow_yaml = _yaml(
            """
            title: Registry lookup
            workflow_definition:
              blocks:
              - block_type: code
                label: block_one
                code: |
                  await page.goto("https://example.com/search")
                  records = [{"number": "REC-001"}]
              - block_type: code
                label: block_two
                code: |
                  await page.goto("https://example.com/results")
            """
        )
        metadata = [
            _terminal_metadata("block_one", "search the registry"),
            {
                "block_label": "block_two",
                "declared_goal": "expand the result rows",
                "claimed_outcomes": [
                    {"id": "claim:rows", "scope": "outcome", "text": "rows expanded", "status": "satisfied"}
                ],
            },
        ]
        ctx = _code_only_ctx()
        result = await _update_workflow({"workflow_yaml": workflow_yaml, "code_artifact_metadata": metadata}, ctx)

        assert result["ok"] is False
        assert "block_two" in result["error"]
        assert "block_one" not in result["error"]
        assert list(ctx.code_artifact_metadata.keys()) == ["block_one"]
        assert ctx.workflow_verification_evidence.code_artifact_metadata == ctx.code_artifact_metadata
        assert "contract violation" not in result["user_facing_summary"]
        assert "`" not in result["user_facing_summary"]

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
    async def test_download_scout_gate_still_blocks_normal_code_only_authoring(self) -> None:
        ctx = _code_only_ctx()
        ctx.scout_trajectory = []

        result = await _update_workflow({"workflow_yaml": self._DOWNLOAD_CODE_YAML}, ctx)

        assert result["ok"] is False
        assert "Scout it first" in result["error"]
        assert "download affordance" in result["error"]

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
    async def test_counter_climbs_on_repeated_recorded_outcome_and_resets_on_accept(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _code_only_ctx()
        for index in range(MAX_CODE_AUTHORING_GUARDRAIL_REJECTS - 1):
            result = await _update_workflow({"workflow_yaml": _distinct_guardrail_yaml(0)}, ctx)
            assert result["ok"] is False
            assert ctx.code_authoring_guardrail_reject_count == index + 1
        assert ctx.blocker_signal is None

        _stub_successful_update(monkeypatch)
        accepted = await _update_workflow({"workflow_yaml": _SAFE_CODE_YAML}, ctx)
        assert accepted["ok"] is True
        assert ctx.code_authoring_guardrail_reject_count == 0

    @pytest.mark.asyncio
    async def test_accepted_persist_at_churn_ceiling_resets_counter_and_clears_held_signals(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _code_only_ctx()
        for _ in range(MAX_CODE_AUTHORING_GUARDRAIL_REJECTS):
            await _update_workflow({"workflow_yaml": _distinct_guardrail_yaml(0)}, ctx)
        assert ctx.code_authoring_guardrail_reject_count == MAX_CODE_AUTHORING_GUARDRAIL_REJECTS
        held = ctx.blocker_signal
        assert isinstance(held, CopilotToolBlockerSignal)
        assert held.internal_reason_code == "code_authoring_guardrail_churn"
        assert ctx.latest_tool_blocker_signal is held

        _stub_successful_update(monkeypatch)
        accepted = await _update_workflow({"workflow_yaml": _SAFE_CODE_YAML}, ctx)

        assert accepted["ok"] is True
        assert ctx.code_authoring_guardrail_reject_count == 0
        assert ctx.blocker_signal is None
        assert ctx.latest_tool_blocker_signal is None

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
    async def test_output_policy_reject_records_author_time_reject_and_guardrail_count(self) -> None:
        ctx = _code_only_ctx()

        result = await _update_workflow(
            {"workflow_yaml": _RAW_SECRET_OUTPUT_POLICY_YAML},
            ctx,
            allow_missing_credentials=True,
        )

        assert result["ok"] is False
        assert "raw_secret_leak" in result["error"]
        outcome = ctx.latest_recorded_build_test_outcome
        assert outcome is not None
        assert outcome.phase == "author_time_reject"
        assert outcome.reason_code == "output_policy_reject"
        assert outcome.is_authoritative is True
        assert outcome.structural_key is not None
        assert "Output policy blocked" in outcome.observed_evidence_summary
        assert ctx.code_authoring_guardrail_reject_count == 1
        assert ctx.blocker_signal is None

    @pytest.mark.asyncio
    async def test_repeated_output_policy_reject_halts_at_guardrail_backstop(self) -> None:
        ctx = _code_only_ctx()

        for _ in range(MAX_CODE_AUTHORING_GUARDRAIL_REJECTS):
            result = await _update_workflow(
                {"workflow_yaml": _RAW_SECRET_OUTPUT_POLICY_YAML},
                ctx,
                allow_missing_credentials=True,
            )
            assert result["ok"] is False

        assert ctx.code_authoring_guardrail_reject_count == MAX_CODE_AUTHORING_GUARDRAIL_REJECTS
        churn = ctx.blocker_signal
        assert isinstance(churn, CopilotToolBlockerSignal)
        assert churn.internal_reason_code == "code_authoring_guardrail_churn"
        assert ctx.latest_tool_blocker_signal is churn
        assert _kind_for_blocker_signal(churn) is TurnHaltKind.LOOP_DETECTED

    @pytest.mark.asyncio
    async def test_nth_reject_stashes_churn_signal_and_resolves_to_loop_halt(self) -> None:
        ctx = _code_only_ctx()
        for _ in range(MAX_CODE_AUTHORING_GUARDRAIL_REJECTS):
            await _update_workflow({"workflow_yaml": _distinct_guardrail_yaml(0)}, ctx)

        assert ctx.code_authoring_guardrail_reject_count == MAX_CODE_AUTHORING_GUARDRAIL_REJECTS
        churn = ctx.blocker_signal
        assert isinstance(churn, CopilotToolBlockerSignal)
        assert churn.internal_reason_code == "code_authoring_guardrail_churn"
        assert ctx.latest_tool_blocker_signal is churn
        assert _kind_for_blocker_signal(churn) is TurnHaltKind.LOOP_DETECTED
        assert "safety checks rejected" in churn.user_facing_reason

    @pytest.mark.asyncio
    async def test_nth_reject_defers_to_pre_existing_terminal_blocker(self) -> None:
        ctx = _code_only_ctx()
        terminal = _terminal_challenge_signal()
        ctx.blocker_signal = terminal
        ctx.latest_tool_blocker_signal = terminal

        for _ in range(MAX_CODE_AUTHORING_GUARDRAIL_REJECTS):
            await _update_workflow({"workflow_yaml": _distinct_guardrail_yaml(0)}, ctx)

        assert ctx.code_authoring_guardrail_reject_count == MAX_CODE_AUTHORING_GUARDRAIL_REJECTS
        assert ctx.blocker_signal is terminal
        assert ctx.latest_tool_blocker_signal is terminal

    def test_churn_stop_defers_while_output_contract_ladder_active_then_fires(self) -> None:
        ctx = _code_only_ctx()
        ctx.output_contract_actuation_count_by_signature["sig_active"] = 1
        for _ in range(MAX_CODE_AUTHORING_GUARDRAIL_REJECTS):
            workflow_update_module._record_code_authoring_guardrail_reject(ctx, frontier_unchanged=True)
        assert ctx.code_authoring_guardrail_reject_count == MAX_CODE_AUTHORING_GUARDRAIL_REJECTS
        assert ctx.blocker_signal is None
        ctx.output_contract_actuation_count_by_signature.clear()
        workflow_update_module._record_code_authoring_guardrail_reject(ctx, frontier_unchanged=True)
        churn = ctx.blocker_signal
        assert isinstance(churn, CopilotToolBlockerSignal)
        assert churn.internal_reason_code == "code_authoring_guardrail_churn"

    def test_churn_stop_defers_while_advisory_grant_held(self) -> None:
        ctx = _code_only_ctx()
        ctx.output_contract_actuation_by_signature["sig_granted"] = OutputContractAdvisoryState.GRANTED
        for _ in range(MAX_CODE_AUTHORING_GUARDRAIL_REJECTS):
            workflow_update_module._record_code_authoring_guardrail_reject(ctx, frontier_unchanged=True)
        assert ctx.blocker_signal is None

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
    async def test_credential_priority_reject_defers_below_higher_bound(self) -> None:
        ctx = _code_only_ctx()
        ctx.scout_trajectory = []

        for index in range(MAX_CREDENTIAL_PRIORITY_AUTHORING_REJECTS - 1):
            await _update_workflow({"workflow_yaml": _page_evaluate_credential_collision_yaml(index)}, ctx)

        assert ctx.code_authoring_guardrail_reject_count == MAX_CREDENTIAL_PRIORITY_AUTHORING_REJECTS - 1
        assert ctx.blocker_signal is None
        assert ctx.latest_tool_blocker_signal is None
        assert _check_enforcement(ctx) is None

    @pytest.mark.asyncio
    async def test_credential_priority_churn_stashes_credential_blocker_at_higher_bound(self) -> None:
        ctx = _code_only_ctx()
        ctx.scout_trajectory = []

        result: dict[str, object] = {}
        for index in range(MAX_CREDENTIAL_PRIORITY_AUTHORING_REJECTS):
            result = await _update_workflow({"workflow_yaml": _page_evaluate_credential_collision_yaml(index)}, ctx)

        assert ctx.code_authoring_guardrail_reject_count == MAX_CREDENTIAL_PRIORITY_AUTHORING_REJECTS
        assert result["ok"] is False
        assert result["data"]["failure_type"] == "missing_credential_or_init"
        assert result["user_facing_summary"] == CREDENTIAL_SCOUT_VERIFY_REPLY
        churn = ctx.blocker_signal
        assert isinstance(churn, CopilotToolBlockerSignal)
        assert churn.internal_reason_code == "credential_priority_authoring_churn"
        assert ctx.latest_tool_blocker_signal is churn
        assert _kind_for_blocker_signal(churn) is TurnHaltKind.LOOP_DETECTED

    @pytest.mark.asyncio
    async def test_credential_priority_churn_renders_credential_scout_reply_through_enforcement(self) -> None:
        ctx = _code_only_ctx()
        ctx.scout_trajectory = []

        for index in range(MAX_CREDENTIAL_PRIORITY_AUTHORING_REJECTS):
            await _update_workflow({"workflow_yaml": _page_evaluate_credential_collision_yaml(index)}, ctx)

        with pytest.raises(CopilotTurnHalt) as excinfo:
            _check_enforcement(ctx)

        assert excinfo.value.halt.kind is TurnHaltKind.LOOP_DETECTED
        churn = ctx.blocker_signal
        assert isinstance(churn, CopilotToolBlockerSignal)
        assert churn.internal_reason_code == "credential_priority_authoring_churn"
        assert churn.user_facing_reason == CREDENTIAL_SCOUT_VERIFY_REPLY

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

    @pytest.mark.asyncio
    async def test_pure_credential_priority_churn_climbs_and_halts(self) -> None:
        ctx = _code_only_ctx()
        ctx.scout_trajectory = []

        result: dict[str, object] = {}
        for index in range(MAX_CREDENTIAL_PRIORITY_AUTHORING_REJECTS - 1):
            result = await _update_workflow({"workflow_yaml": _safe_credential_collision_yaml(index)}, ctx)
            assert result["ok"] is False
            assert result["data"]["failure_type"] == "missing_credential_or_init"
            assert "diagnostic_code_safety_errors" not in result["data"]
            assert result["user_facing_summary"] == CREDENTIAL_SCOUT_VERIFY_REPLY
            assert ctx.code_authoring_guardrail_reject_count == index + 1

        assert ctx.blocker_signal is None
        assert _check_enforcement(ctx) is None

        result = await _update_workflow(
            {"workflow_yaml": _safe_credential_collision_yaml(MAX_CREDENTIAL_PRIORITY_AUTHORING_REJECTS - 1)}, ctx
        )

        assert ctx.code_authoring_guardrail_reject_count == MAX_CREDENTIAL_PRIORITY_AUTHORING_REJECTS
        assert result["user_facing_summary"] == CREDENTIAL_SCOUT_VERIFY_REPLY
        churn = ctx.blocker_signal
        assert isinstance(churn, CopilotToolBlockerSignal)
        assert churn.internal_reason_code == "credential_priority_authoring_churn"
        with pytest.raises(CopilotTurnHalt) as excinfo:
            _check_enforcement(ctx)
        assert excinfo.value.halt.kind is TurnHaltKind.LOOP_DETECTED
        assert churn.user_facing_reason == CREDENTIAL_SCOUT_VERIFY_REPLY

    @pytest.mark.asyncio
    async def test_name_safety_churn_still_halts_after_credential_priority_reject(self) -> None:
        ctx = _code_only_ctx()
        ctx.scout_trajectory = []

        for index in range(MAX_CODE_AUTHORING_GUARDRAIL_REJECTS - 1):
            await _update_workflow({"workflow_yaml": _page_evaluate_credential_collision_yaml(index)}, ctx)
        assert ctx.last_code_authoring_reject_was_credential_priority is True

        ctx.scout_trajectory = [
            {
                "tool_name": "click",
                "selector": "#search-submit",
                "source_url": "https://example.com/search",
                "trajectory_index": 0,
            }
        ]
        await _update_workflow({"workflow_yaml": _distinct_guardrail_yaml(0)}, ctx)

        assert ctx.code_authoring_guardrail_reject_count == 1
        assert ctx.last_code_authoring_reject_was_credential_priority is False
        assert ctx.blocker_signal is None
        assert _check_enforcement(ctx) is None

    @pytest.mark.asyncio
    async def test_non_credential_reject_resets_when_recorded_outcome_changes_after_precharge(self) -> None:
        ctx = _code_only_ctx()
        ctx.scout_trajectory = []

        for index in range(MAX_CODE_AUTHORING_GUARDRAIL_REJECTS + 1):
            await _update_workflow({"workflow_yaml": _safe_credential_collision_yaml(index)}, ctx)
        assert ctx.code_authoring_guardrail_reject_count == MAX_CODE_AUTHORING_GUARDRAIL_REJECTS + 1
        assert ctx.last_code_authoring_reject_was_credential_priority is True
        assert ctx.blocker_signal is None
        assert _check_enforcement(ctx) is None

        ctx.scout_trajectory = [
            {
                "tool_name": "click",
                "selector": "#search-submit",
                "source_url": "https://example.com/search",
                "trajectory_index": 0,
            }
        ]
        await _update_workflow({"workflow_yaml": _distinct_guardrail_yaml(0)}, ctx)

        assert ctx.code_authoring_guardrail_reject_count == 1
        assert ctx.last_code_authoring_reject_was_credential_priority is False
        assert ctx.blocker_signal is None
        assert _check_enforcement(ctx) is None

    @pytest.mark.asyncio
    async def test_changed_authoring_recorded_outcome_resets_guardrail_backstop(self) -> None:
        ctx = _code_only_ctx()

        await _update_workflow({"workflow_yaml": _code_yaml("value = missing_first_name()")}, ctx)
        first_key = ctx.latest_recorded_build_test_outcome.structural_key
        await _update_workflow({"workflow_yaml": _code_yaml("value = missing_second_name()")}, ctx)

        assert first_key is not None
        assert ctx.latest_recorded_build_test_outcome.structural_key != first_key
        assert ctx.code_authoring_guardrail_reject_count == 1
        assert ctx.blocker_signal is None

    @pytest.mark.asyncio
    async def test_identical_authoring_recorded_outcome_still_halts_at_backstop(self) -> None:
        ctx = _code_only_ctx()
        for _ in range(MAX_CODE_AUTHORING_GUARDRAIL_REJECTS):
            await _update_workflow({"workflow_yaml": _code_yaml("value = missing_first_name()")}, ctx)

        assert ctx.code_authoring_guardrail_reject_count == MAX_CODE_AUTHORING_GUARDRAIL_REJECTS
        churn = ctx.blocker_signal
        assert isinstance(churn, CopilotToolBlockerSignal)
        assert churn.internal_reason_code == "code_authoring_guardrail_churn"

    @pytest.mark.asyncio
    async def test_accept_after_latched_churn_clears_both_signals(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx = _code_only_ctx()
        for _ in range(MAX_CODE_AUTHORING_GUARDRAIL_REJECTS):
            await _update_workflow({"workflow_yaml": _distinct_guardrail_yaml(0)}, ctx)
        churn = ctx.blocker_signal
        assert isinstance(churn, CopilotToolBlockerSignal)
        assert churn.internal_reason_code == "code_authoring_guardrail_churn"
        assert ctx.latest_tool_blocker_signal is churn

        _stub_successful_update(monkeypatch)
        accepted = await _update_workflow({"workflow_yaml": _SAFE_CODE_YAML}, ctx)

        assert accepted["ok"] is True
        assert ctx.code_authoring_guardrail_reject_count == 0
        assert ctx.blocker_signal is None
        assert ctx.latest_tool_blocker_signal is None
        assert _check_enforcement(ctx) is None


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
                  await page.locator("#coverage-next").click()
              - block_type: code
                label: invented_browser_step
                code: |
                  await page.locator("#electricDate").fill("2026-07-01")
            """
        )

        result = workflow_update_module._maybe_impose_synthesized_code_block(submitted, ctx)

        assert any("unscouted browser action" in violation for violation in result.violations)
        assert result.substitutions is None

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
    def test_rejects_ambiguous_browser_mutation(self, sibling_code: str) -> None:
        ctx = _quote_ctx()
        submitted = _submitted_with_sibling_code(sibling_code)

        result = workflow_update_module._maybe_impose_synthesized_code_block(submitted, ctx)

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

    def test_rejects_ambiguous_helper_browser_mutation(self) -> None:
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

        assert any("ambiguous browser action" in violation for violation in result.violations)
        assert result.substitutions is None

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
    async def test_assigned_parameter_key_does_not_preserve_submitted_extraction(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _quote_ctx()
        synthesized = workflow_update_module.synthesize_code_block(ctx.scout_trajectory, strict_selectors=True)
        assert synthesized is not None
        submitted_code = (
            'zip_code = "99999"\n'
            + textwrap.dedent(synthesized.code).rstrip()
            + f'\n{workflow_update_module._SYNTHESIZED_BLOCK_LABEL}_output = {{"quote": "Q-001"}}\n'
            + f"return {workflow_update_module._SYNTHESIZED_BLOCK_LABEL}_output\n"
        )
        submitted = _yaml(
            f"""
            title: Quote
            workflow_definition:
              parameters:
              - {{parameter_type: workflow, workflow_parameter_type: string, key: zip_code, default_value: "02110"}}
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

        assert result["ok"] is False
        assert "assigns synthesized parameter key(s) `zip_code`" in result["error"]

    @pytest.mark.asyncio
    async def test_destructured_parameter_key_does_not_preserve_submitted_extraction(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _quote_ctx()
        synthesized = workflow_update_module.synthesize_code_block(ctx.scout_trajectory, strict_selectors=True)
        assert synthesized is not None
        submitted_code = (
            '(zip_code,) = ("99999",)\n'
            + textwrap.dedent(synthesized.code).rstrip()
            + f'\n{workflow_update_module._SYNTHESIZED_BLOCK_LABEL}_output = {{"quote": "Q-001"}}\n'
            + f"return {workflow_update_module._SYNTHESIZED_BLOCK_LABEL}_output\n"
        )
        submitted = _yaml(
            f"""
            title: Quote
            workflow_definition:
              parameters:
              - {{parameter_type: workflow, workflow_parameter_type: string, key: zip_code, default_value: "02110"}}
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

        assert result["ok"] is False
        assert "assigns synthesized parameter key(s) `zip_code`" in result["error"]

    @pytest.mark.asyncio
    async def test_starred_parameter_key_does_not_preserve_submitted_extraction(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _quote_ctx()
        synthesized = workflow_update_module.synthesize_code_block(ctx.scout_trajectory, strict_selectors=True)
        assert synthesized is not None
        submitted_code = (
            '*zip_code, = ["99999"]\n'
            + textwrap.dedent(synthesized.code).rstrip()
            + f'\n{workflow_update_module._SYNTHESIZED_BLOCK_LABEL}_output = {{"quote": "Q-001"}}\n'
            + f"return {workflow_update_module._SYNTHESIZED_BLOCK_LABEL}_output\n"
        )
        submitted = _yaml(
            f"""
            title: Quote
            workflow_definition:
              parameters:
              - {{parameter_type: workflow, workflow_parameter_type: string, key: zip_code, default_value: "02110"}}
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

        assert result["ok"] is False
        assert "assigns synthesized parameter key(s) `zip_code`" in result["error"]

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
        assert all(stage.get("parameter_keys") == ["postal_code"] for stage in code_blocks[:-1])
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

    @pytest.mark.asyncio
    async def test_author_parameter_reject_reopens_and_preserves_typed_violation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
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
        ctx.update_workflow_called = True
        ctx.latest_recorded_build_test_outcome = _author_time_reject_outcome("synthesized_parameter_binding_ambiguous")
        ctx.workflow_yaml = _SUBMITTED_LITERAL_YAML

        result = await _update_workflow({"workflow_yaml": _SUBMITTED_UNKNOWN_COMPUTED_LITERAL_YAML}, ctx)

        assert result["ok"] is False
        assert "Unable to bind synthesized parameter `provider_name`" in result["error"]
        repair = result["data"]["authoring_repair_context"]
        assert repair["reason_code"] == "synthesized_parameter_binding_ambiguous"
        assert repair["unresolved_names"] == ["provider_name"]
        assert ctx.workflow_yaml == _SUBMITTED_LITERAL_YAML

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
    async def test_stale_selected_goal_paths_do_not_block_imposed_scout_spine(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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
        assert result["data"]["imposed_substitutions"]["scrubbed_stale_selected_goal_value_paths"] is True
        parsed = parse_workflow_yaml(ctx.workflow_yaml)
        assert isinstance(parsed, dict)
        code = str(_single_code_block(parsed)["code"])
        assert "#electricDate" not in code
        assert 'await page.locator("#zip").fill(str(zip_code))' in code
        artifact = ctx.code_artifact_metadata["quote_flow"]
        assert not workflow_update_module._artifact_declares_goal_values(artifact)

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

    @pytest.mark.asyncio
    async def test_invalid_selected_extraction_suffix_keeps_goal_path_rejection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _quote_ctx()
        synthesized = workflow_update_module.synthesize_code_block(ctx.scout_trajectory, strict_selectors=True)
        assert synthesized is not None
        submitted_code = textwrap.dedent(synthesized.code).rstrip() + "\nheading = 'Review'\n"
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

        assert result["ok"] is False
        assert "does not return a keyed structure" in result["error"]
        assert "records" in result["error"]

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
    async def test_sibling_invalid_goal_paths_still_reject(self, monkeypatch: pytest.MonkeyPatch) -> None:
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
                  await page.locator("#zip").fill(str(zip_code))
                  await page.locator("#continue").click()
              - block_type: code
                label: summarize_quote
                code: |
                  return await page.locator("h1").inner_text()
            """
        )

        result = await _update_workflow(
            {"workflow_yaml": submitted, "code_artifact_metadata": [_terminal_metadata("summarize_quote", "summary")]},
            ctx,
        )

        assert result["ok"] is False
        assert "summarize_quote" in result["error"]
        assert "flat text blob" in result["error"]

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
            workflow_update_module._stable_same_kind_bare_click_refiner(
                "button", "xpath=//button[normalize-space()='Check Order Status']"
            )
            is True
        )
        assert workflow_update_module._stable_same_kind_bare_click_refiner("button", 'role=button[name="Next"]') is True
        for candidate in (
            "xpath=//a[normalize-space()='Check Order Status']",
            'role=link[name="Next"]',
            "xpath=(//button[normalize-space()='Check Order Status'])[2]",
            "xpath=//button[contains(normalize-space(), 'Check')]",
        ):
            assert workflow_update_module._stable_same_kind_bare_click_refiner("button", candidate) is False

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


class TestDeclarationContractStamp:
    def test_zero_output_block_persists_declared_blocker_at_ceiling(self) -> None:
        ctx = _declaration_stamp_ctx()
        signature = workflow_update_module._stable_output_contract_key(
            "turn:t-decl", {"output.record_id", "output.blocker"}
        )
        ctx.output_contract_reject_count_by_signature = {
            signature: workflow_update_module._MAX_OUTPUT_CONTRACT_STEERING_REJECTS
        }
        workflow_yaml = _collapsed_spine_yaml('await page.click("#submit")')

        new_yaml, _metadata, applied = workflow_update_module._impose_output_contract_envelope_after_steering(
            ctx, workflow_yaml, []
        )

        assert applied is True
        code = str(workflow_blocks(parse_workflow_yaml(new_yaml))[0].get("code") or "")
        produced = workflow_update_module._code_block_produced_output_paths(code)
        assert "output.blocker" in produced  # nosemgrep: incomplete-url-substring-sanitization
        assert "output.record_id" not in produced
        assert '"blocker": None' in code

    def test_stamp_applies_before_steering_reject_wait(self) -> None:
        ctx = _declaration_stamp_ctx()
        workflow_yaml = _collapsed_spine_yaml('await page.click("#submit")\nreturn {}')

        new_yaml, _metadata, applied = workflow_update_module._impose_output_contract_envelope_after_steering(
            ctx, workflow_yaml, []
        )

        assert applied is True
        code = str(workflow_blocks(parse_workflow_yaml(new_yaml))[0].get("code") or "")
        assert 'return {"output": {"blocker": None}}' in code

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

    @pytest.mark.asyncio
    async def test_under_build_rejects_climb_churn_counter_to_stop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            workflow_update_module, "synthesize_code_block", lambda *a, **k: _drifted_spine_synthesized()
        )
        ctx = _quote_ctx()
        workflow_yaml = _under_build_draft_yaml()

        for index in range(MAX_CODE_AUTHORING_GUARDRAIL_REJECTS - 1):
            result = await _update_workflow({"workflow_yaml": workflow_yaml}, ctx)
            assert result["ok"] is False
            assert "scouted_spine_under_build" in result["error"]
            assert ctx.code_authoring_guardrail_reject_count == index + 1
        assert ctx.blocker_signal is None

        final = await _update_workflow({"workflow_yaml": workflow_yaml}, ctx)

        assert final["ok"] is False
        assert ctx.code_authoring_guardrail_reject_count == MAX_CODE_AUTHORING_GUARDRAIL_REJECTS
        held = ctx.blocker_signal
        assert isinstance(held, CopilotToolBlockerSignal)
        assert held.internal_reason_code == "code_authoring_guardrail_churn"


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
    @pytest.mark.asyncio
    async def test_early_partial_persist_then_stub_resubmission_rejected_and_turn_end_fires(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _records_spine_ctx()
        full_trajectory = list(ctx.scout_trajectory)
        ctx.scout_trajectory = full_trajectory[:2]
        partial_synthesized = workflow_update_module.synthesize_code_block(ctx.scout_trajectory, strict_selectors=True)
        assert partial_synthesized is not None

        persisted = await _update_workflow({"workflow_yaml": _records_block_yaml(partial_synthesized.code)}, ctx)

        assert persisted["ok"] is True
        assert ctx.persisted_draft_browser_calls is not None
        assert [pair for pair in ctx.persisted_draft_browser_calls if pair[0] == "click"] == [
            ("click", "page.locator('#stage-a')"),
            ("click", "page.locator('#stage-b')"),
        ]

        ctx.scout_trajectory = full_trajectory

        with capture_logs() as logs:
            rejected = await _update_workflow({"workflow_yaml": _records_block_yaml(partial_synthesized.code)}, ctx)

        assert rejected["ok"] is False
        assert "scouted_spine_under_build" in rejected["error"]
        assert 'await page.locator("#stage-c").click()' in rejected["error"]
        events = [log for log in logs if log["event"] == "copilot_scouted_spine_under_build"]
        assert len(events) == 1
        assert events[0]["site"] == "persist_seam"

        with capture_logs() as logs:
            nudge = enforcement_module._scouted_spine_turn_end_nudge(ctx)

        assert nudge is not None
        assert "scouted_spine_under_build" in nudge
        assert 'await page.locator("#stage-c").click()' in nudge
        turn_end_events = [log for log in logs if log["event"] == "copilot_scouted_spine_under_build"]
        assert len(turn_end_events) == 1
        assert turn_end_events[0]["site"] == "turn_end"

    @pytest.mark.asyncio
    async def test_post_update_skip_path_persist_under_coverage_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _records_spine_ctx()
        ctx.update_workflow_called = True

        with capture_logs() as logs:
            result = await _update_workflow(
                {"workflow_yaml": _records_block_yaml('await page.locator("#stage-a").click()')}, ctx
            )

        assert result["ok"] is False
        assert "scouted_spine_under_build" in result["error"]
        assert 'await page.locator("#stage-b").click()' in result["error"]
        assert 'await page.locator("#stage-c").click()' in result["error"]
        assert ctx.code_authoring_guardrail_reject_count == 1
        assert [log for log in logs if log["event"] == "copilot_imposition_skipped_after_update"]
        events = [log for log in logs if log["event"] == "copilot_scouted_spine_under_build"]
        assert len(events) == 1
        assert events[0]["site"] == "persist_seam"

    @pytest.mark.asyncio
    async def test_second_persist_dropping_all_code_blocks_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _checkpoint_eligible_ctx()
        no_code_yaml = _yaml(
            """
            title: Records
            workflow_definition:
              blocks:
              - block_type: send_email
                label: notify
                recipients:
                - ops@example.com
                subject: Records ready
                body: Records run finished
            """
        )

        with capture_logs() as logs:
            result = await _update_workflow({"workflow_yaml": no_code_yaml}, ctx)

        assert result["ok"] is False
        assert "scouted_spine_under_build" in result["error"]
        assert 'await page.locator("#stage-a").click()' in result["error"]
        assert 'await page.locator("#stage-c").click()' in result["error"]
        assert ctx.code_authoring_guardrail_reject_count == 1
        events = [log for log in logs if log["event"] == "copilot_scouted_spine_under_build"]
        assert len(events) == 1
        assert events[0]["site"] == "persist_seam"

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
    async def test_verbatim_synthesized_resubmission_clears_coverage_and_persists(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _records_spine_ctx()
        ctx.update_workflow_called = True
        ctx.persisted_draft_browser_calls = [("click", 'page.locator("#stage-a")')]
        synthesized = workflow_update_module.synthesize_code_block(ctx.scout_trajectory, strict_selectors=True)
        assert synthesized is not None

        with capture_logs() as logs:
            result = await _update_workflow({"workflow_yaml": _records_block_yaml(synthesized.code)}, ctx)

        assert result["ok"] is True
        assert not [log for log in logs if log["event"] == "copilot_scouted_spine_under_build"]
        assert ctx.persisted_draft_browser_calls is not None
        assert [pair for pair in ctx.persisted_draft_browser_calls if pair[0] == "click"] == [
            ("click", "page.locator('#stage-a')"),
            ("click", "page.locator('#stage-b')"),
            ("click", "page.locator('#stage-c')"),
        ]
        assert enforcement_module._scouted_spine_turn_end_nudge(ctx) is None

    @pytest.mark.asyncio
    async def test_composite_route_resubmission_passes_credential_gate_and_persists(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        monkeypatch.setattr(
            workflow_update_module, "_credential_reference_validation_error", AsyncMock(return_value=None)
        )
        ctx = _credential_spine_ctx()

        with capture_logs() as logs:
            rejected = await _update_workflow(
                {"workflow_yaml": _records_block_yaml('await page.locator("#stage-a").click()')}, ctx
            )

        assert rejected["ok"] is False
        assert "scouted_spine_under_build" in rejected["error"]
        assert "fill_credential_field" in rejected["error"]
        assert "click the submit control or press Enter" in rejected["error"]
        assert "Missing rung source to reuse verbatim" in rejected["error"]
        events = [log for log in logs if log["event"] == "copilot_scouted_spine_under_build"]
        assert len(events) == 1
        assert events[0]["site"] == "persist_seam"
        assert events[0]["credential_scout_precondition_pending"] is True

        ctx.scout_trajectory.append(_submit_interaction(source_url="https://example.com/records"))
        synthesized = workflow_update_module.synthesize_code_block(ctx.scout_trajectory, strict_selectors=True)
        assert synthesized is not None

        with capture_logs() as logs:
            accepted = await _update_workflow({"workflow_yaml": _credential_spine_block_yaml(synthesized)}, ctx)

        assert accepted["ok"] is True
        assert not [log for log in logs if log["event"] == "copilot_scouted_spine_under_build"]
        assert enforcement_module._scouted_spine_turn_end_nudge(ctx) is None

    @pytest.mark.asyncio
    async def test_satisfied_credential_precondition_keeps_single_step_route(self) -> None:
        ctx = _credential_spine_ctx()
        ctx.scout_trajectory.append(_submit_interaction(source_url="https://example.com/records"))

        with capture_logs() as logs:
            rejected = await _update_workflow(
                {"workflow_yaml": _records_block_yaml('await page.locator("#stage-a").click()')}, ctx
            )

        assert rejected["ok"] is False
        assert "scouted_spine_under_build" in rejected["error"]
        assert "fill_credential_field" not in rejected["error"]
        assert "Missing rung source to reuse verbatim" in rejected["error"]
        events = [log for log in logs if log["event"] == "copilot_scouted_spine_under_build"]
        assert len(events) == 1
        assert events[0]["credential_scout_precondition_pending"] is False

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


class TestScoutedSpineTurnEndCheckpoint:
    def test_checkpoint_is_single_shot_and_emits_unresolved_when_spent(self) -> None:
        ctx = _checkpoint_eligible_ctx()

        first = enforcement_module._scouted_spine_turn_end_nudge(ctx)
        assert first is not None
        assert "scouted_spine_under_build" in first
        assert 'await page.locator("#stage-b").click()' in first
        assert ctx.code_authoring_guardrail_reject_count == 1

        with capture_logs() as logs:
            second = enforcement_module._scouted_spine_turn_end_nudge(ctx)

        assert second is None
        assert ctx.code_authoring_guardrail_reject_count == 1
        unresolved = [log for log in logs if log["event"] == "copilot_scouted_spine_under_build_unresolved"]
        assert len(unresolved) == 1
        assert unresolved[0]["site"] == "turn_end"

    def test_checkpoint_reject_threads_churn_and_ceiling_emits_unresolved(self) -> None:
        ctx = _checkpoint_eligible_ctx()
        seed_repair = CodeAuthoringRepairContext(
            block_label="persisted_draft",
            reason_code="scouted_spine_under_build",
            selector="#stage-b",
        )
        record_build_test_outcome(ctx, recorded_outcome_from_authoring_repair_context(seed_repair))
        ctx.code_authoring_guardrail_reject_count = MAX_CODE_AUTHORING_GUARDRAIL_REJECTS - 1

        with capture_logs() as logs:
            nudge = enforcement_module._scouted_spine_turn_end_nudge(ctx)

        assert nudge is not None
        assert ctx.code_authoring_guardrail_reject_count == MAX_CODE_AUTHORING_GUARDRAIL_REJECTS
        held = ctx.blocker_signal
        assert isinstance(held, CopilotToolBlockerSignal)
        assert held.internal_reason_code == "code_authoring_guardrail_churn"
        unresolved = [log for log in logs if log["event"] == "copilot_scouted_spine_under_build_unresolved"]
        assert len(unresolved) == 1
        assert unresolved[0]["site"] == "churn_stop"

        assert enforcement_module._scouted_spine_turn_end_nudge(ctx) is None
        assert ctx.code_authoring_guardrail_reject_count == MAX_CODE_AUTHORING_GUARDRAIL_REJECTS

    def test_no_spurious_checkpoint_on_full_coverage_or_lane_only_remainder(self) -> None:
        full = _checkpoint_eligible_ctx()
        full.persisted_draft_browser_calls = [
            ("click", 'page.locator("#stage-a")'),
            ("click", 'page.locator("#stage-b")'),
            ("click", 'page.locator("#stage-c")'),
        ]
        with capture_logs() as logs:
            assert enforcement_module._scouted_spine_turn_end_nudge(full) is None
        assert not [log for log in logs if "scouted_spine" in str(log.get("event"))]
        assert full.code_authoring_guardrail_reject_count == 0

        lane_only = _checkpoint_eligible_ctx()
        lane_only.scout_trajectory = lane_only.scout_trajectory[:1] + [
            {
                "tool_name": "click",
                "selector": "#cookie-accept",
                "role": "button",
                "accessible_name": "Accept all cookies",
                "source_url": "https://example.com/records",
                "trajectory_index": 1,
            }
        ]
        with capture_logs() as logs:
            assert enforcement_module._scouted_spine_turn_end_nudge(lane_only) is None
        assert not [log for log in logs if "scouted_spine" in str(log.get("event"))]

        no_persist = _records_spine_ctx()
        assert enforcement_module._scouted_spine_turn_end_nudge(no_persist) is None

        standard = _checkpoint_eligible_ctx()
        standard.block_authoring_policy = BlockAuthoringPolicy.STANDARD
        assert enforcement_module._scouted_spine_turn_end_nudge(standard) is None


def _full_coverage_calls() -> list[tuple[str, str]]:
    return [
        ("click", 'page.locator("#stage-a")'),
        ("click", 'page.locator("#stage-b")'),
        ("click", 'page.locator("#stage-c")'),
    ]


class TestScoutedSpineTurnHaltExit:
    def test_repair_ceiling_halt_with_open_obligation_emits_unresolved_and_reframes_reply(self) -> None:
        ctx = _checkpoint_eligible_ctx()
        ctx.last_test_anti_bot = "challenge_detected"
        signal = enforcement_module.repair_ceiling_stop_signal(ctx, None)
        assert "verification challenge" in signal.user_facing_reason
        ctx.blocker_signal = signal
        halt = TurnHalt(kind=TurnHaltKind.REPAIR_CEILING_REACHED, blocker_signal=signal)

        with capture_logs() as logs:
            result = agent_module._build_turn_halt_exit_result(ctx, global_llm_context=None, halt=halt)

        unresolved = [log for log in logs if log["event"] == "copilot_scouted_spine_under_build_unresolved"]
        assert len(unresolved) == 1
        assert unresolved[0]["site"] == "turn_halt"
        assert result.user_response == enforcement_module.SCOUTED_SPINE_TURN_HALT_USER_REASON
        assert "verification challenge" not in result.user_response

    def test_site_block_halt_with_open_obligation_emits_unresolved_and_keeps_site_block_reply(self) -> None:
        ctx = _checkpoint_eligible_ctx()
        signal = enforcement_module._probable_site_block_stop_signal(ctx)
        ctx.blocker_signal = signal
        halt = TurnHalt(kind=TurnHaltKind.PROBABLE_SITE_BLOCK, blocker_signal=signal)

        with capture_logs() as logs:
            result = agent_module._build_turn_halt_exit_result(ctx, global_llm_context=None, halt=halt)

        unresolved = [log for log in logs if log["event"] == "copilot_scouted_spine_under_build_unresolved"]
        assert len(unresolved) == 1
        assert unresolved[0]["site"] == "turn_halt"
        assert result.user_response == signal.user_facing_reason

    def test_delivered_unverified_halt_with_open_obligation_emits_unresolved(self) -> None:
        ctx = _checkpoint_eligible_ctx()
        halt = TurnHalt(
            kind=TurnHaltKind.DELIVERED_UNVERIFIED,
            verdict=TurnHaltVerdict.DELIVERED_UNVERIFIED,
        )

        with capture_logs() as logs:
            agent_module._build_turn_halt_exit_result(ctx, global_llm_context=None, halt=halt)

        unresolved = [log for log in logs if log["event"] == "copilot_scouted_spine_under_build_unresolved"]
        assert len(unresolved) == 1
        assert unresolved[0]["site"] == "turn_halt"

    def test_halt_without_open_obligation_emits_nothing_and_keeps_reply(self) -> None:
        ctx = _checkpoint_eligible_ctx()
        ctx.persisted_draft_browser_calls = _full_coverage_calls()
        signal = enforcement_module.repair_ceiling_stop_signal(ctx, None)
        ctx.blocker_signal = signal
        halt = TurnHalt(kind=TurnHaltKind.REPAIR_CEILING_REACHED, blocker_signal=signal)

        with capture_logs() as logs:
            result = agent_module._build_turn_halt_exit_result(ctx, global_llm_context=None, halt=halt)

        assert not [log for log in logs if "scouted_spine" in str(log.get("event"))]
        assert result.user_response == signal.user_facing_reason

    @pytest.mark.asyncio
    async def test_wrapped_exception_exit_with_stashed_repair_ceiling_halt_emits_unresolved(self) -> None:
        ctx = _checkpoint_eligible_ctx()
        signal = enforcement_module.repair_ceiling_stop_signal(ctx, None)
        stash_repair_ceiling_turn_halt(ctx, signal, consecutive_identical_repair_count=3)
        ctx.blocker_signal = signal

        with capture_logs() as logs:
            result = await agent_module._resolve_wrapped_exception_exit_result(
                ctx,
                None,
                goal_satisfied=False,
                error=RuntimeError("wrapped"),
                workflow_permanent_id="wp",
            )

        unresolved = [log for log in logs if log["event"] == "copilot_scouted_spine_under_build_unresolved"]
        assert len(unresolved) == 1
        assert unresolved[0]["site"] == "turn_halt"
        assert result.user_response == enforcement_module.SCOUTED_SPINE_TURN_HALT_USER_REASON

    def test_obligation_check_failure_never_blocks_the_halt_reply(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx = _checkpoint_eligible_ctx()
        signal = enforcement_module.repair_ceiling_stop_signal(ctx, None)
        ctx.blocker_signal = signal
        halt = TurnHalt(kind=TurnHaltKind.REPAIR_CEILING_REACHED, blocker_signal=signal)

        def _boom(*args: object, **kwargs: object) -> None:
            raise RuntimeError("synthesis unavailable")

        monkeypatch.setattr(enforcement_module, "synthesize_code_block", _boom)

        with capture_logs() as logs:
            result = agent_module._build_turn_halt_exit_result(ctx, global_llm_context=None, halt=halt)

        assert not [log for log in logs if log["event"] == "copilot_scouted_spine_under_build_unresolved"]
        assert result.user_response == signal.user_facing_reason


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
