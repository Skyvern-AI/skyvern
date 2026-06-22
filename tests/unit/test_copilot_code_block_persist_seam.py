"""Tests for the code-block persist seam in `_update_workflow`.

OSS-synced: only example.* / RFC-2606 placeholder targets and synthetic labels.
"""

from __future__ import annotations

import ast
import textwrap
from types import SimpleNamespace

import pytest
import yaml

from skyvern.forge.sdk.copilot.blocker_signal import (
    CREDENTIAL_SCOUT_VERIFY_REPLY,
    CopilotToolBlockerSignal,
    assert_clean_user_facing_text,
)
from skyvern.forge.sdk.copilot.code_block_preflight import _sandbox_shim_surface, strip_redundant_sandbox_imports
from skyvern.forge.sdk.copilot.code_block_synthesis import _get_by_role_expr, _get_by_role_expr_strict
from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.enforcement import (
    MAX_CODE_AUTHORING_GUARDRAIL_REJECTS,
    MAX_CREDENTIAL_PRIORITY_AUTHORING_REJECTS,
    _check_enforcement,
)
from skyvern.forge.sdk.copilot.reached_download_target import ReachedDownloadTarget
from skyvern.forge.sdk.copilot.request_policy import RequestPolicy
from skyvern.forge.sdk.copilot.run_outcome import TERMINAL_CHALLENGE_BLOCKER_REASON_CODE
from skyvern.forge.sdk.copilot.runtime import AgentContext
from skyvern.forge.sdk.copilot.tools import (
    _code_block_safety_errors,
    _detect_stale_block_metadata,
    _update_workflow,
)
from skyvern.forge.sdk.copilot.tools import scouting as scouting_module
from skyvern.forge.sdk.copilot.tools import workflow_update as workflow_update_module
from skyvern.forge.sdk.copilot.tools.workflow_update import _strip_redundant_sandbox_imports_in_yaml
from skyvern.forge.sdk.copilot.turn_halt import CopilotTurnHalt, TurnHaltKind, _kind_for_blocker_signal
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
    def _fake_process_workflow_yaml(**_kwargs: object) -> SimpleNamespace:
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


class TestCodeBlockParameterPersistSeam:
    @pytest.mark.asyncio
    async def test_undeclared_parameter_key_rejects_before_persist(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        ctx.workflow_yaml = _SAFE_CODE_YAML
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

        assert result["ok"] is False
        assert "does not return a keyed structure" in result["error"]
        assert ctx.workflow_yaml == ""

    @pytest.mark.asyncio
    async def test_output_intent_requires_artifact_metadata_before_persist(
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
        block = _single_code_block(parsed)
        assert "<fill" not in block["code"]
        assert "<fill: captured value>" not in block["code"]
        assert 'await page.locator("#provInput").fill(str(provider_name))' in block["code"]
        assert 'records = [{"number": "REC-001", "status": "credentialed"}]' in block["code"]

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

        assert result["ok"] is True
        assert result["data"]["imposed_substitutions"]["preserved_submitted_extraction_code"] is True
        parsed = parse_workflow_yaml(ctx.workflow_yaml)
        assert isinstance(parsed, dict)
        block = _single_code_block(parsed)
        assert block["code"].startswith("records = []")
        assert 'await page.locator("#provInput").fill(str(provider_name))' in block["code"]
        assert 'records.append({"number": "REC-001", "status": "credentialed"})' in block["code"]

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

        result = await _update_workflow({"workflow_yaml": _SUBMITTED_MIXED_LOCATOR_FILL_COMPUTED_LITERAL_YAML}, ctx)

        assert result["ok"] is False
        assert "submitted code mixes direct fills using `provider_name`" in result["error"]
        assert ctx.workflow_yaml == ""

    @pytest.mark.asyncio
    async def test_unknown_computed_parameter_rejects_before_persist(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_successful_update(monkeypatch)
        ctx = self._provider_search_ctx()

        result = await _update_workflow({"workflow_yaml": _SUBMITTED_UNKNOWN_COMPUTED_LITERAL_YAML}, ctx)

        assert result["ok"] is False
        assert "Unable to bind synthesized parameter `provider_name`" in result["error"]
        assert ctx.workflow_yaml == ""

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
    async def test_synthesized_parameter_aliases_by_typed_length_to_declared_default(
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

        assert result["ok"] is True
        parsed = parse_workflow_yaml(ctx.workflow_yaml)
        assert isinstance(parsed, dict)
        block = _single_code_block(parsed)
        assert block["parameter_keys"] == ["search_location", "provider_first_name"]
        assert "str(search_location)" in block["code"]
        assert "address_or_postal_code" not in block["code"]
        assert result["data"]["imposed_substitutions"]["parameter_aliases"] == {
            "address_or_postal_code": "search_location"
        }

    @pytest.mark.asyncio
    async def test_synthesized_provider_search_key_rewrites_to_declared_first_last_inputs(
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

        assert result["ok"] is True
        parsed = parse_workflow_yaml(ctx.workflow_yaml)
        assert isinstance(parsed, dict)
        block = _single_code_block(parsed)
        assert block["parameter_keys"] == ["search_location", "provider_first_name", "provider_last_name"]
        assert "str(search_location)" in block["code"]
        assert '(str(provider_first_name) + " " + str(provider_last_name))' in block["code"]
        assert "address_or_postal_code" not in block["code"]
        assert "provider_name_or_identifier" not in block["code"]
        assert result["data"]["imposed_substitutions"]["parameter_aliases"] == {
            "address_or_postal_code": "search_location"
        }
        assert result["data"]["imposed_substitutions"]["parameter_expressions"] == {
            "provider_name_or_identifier": ('(str(provider_first_name) + " " + str(provider_last_name))')
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
            '(str(provider_first_name) + " " + str(provider_last_name))',
        )

        ast.parse(rewritten)
        assert 'str((str(provider_first_name) + " " + str(provider_last_name)))' in rewritten
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

    def test_ambiguous_combined_default_match_logs_and_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        debug_events: list[tuple[str, dict[str, object]]] = []
        monkeypatch.setattr(
            workflow_update_module,
            "LOG",
            SimpleNamespace(debug=lambda event, **kwargs: debug_events.append((event, kwargs))),
        )

        result = workflow_update_module._combined_string_default_expression(
            [
                {"key": "first_name", "default_value": "Given"},
                {"key": "last_name", "default_value": "Family"},
                {"key": "given_name", "default_value": "Given"},
                {"key": "family_name", "default_value": "Family"},
            ],
            synthesized_default="Given Family",
            typed_length=None,
        )

        assert result is None
        assert debug_events == [
            (
                "copilot_synthesized_parameter_combined_default_ambiguous",
                {"match_count": 3, "synthesized_default_present": True, "typed_length": None},
            )
        ]

    @pytest.mark.asyncio
    async def test_synthesized_provider_search_key_rewrites_to_short_first_last_inputs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = _code_only_ctx()
        _enable_imposition(ctx)
        ctx.scout_trajectory = [
            {
                "tool_name": "type_text",
                "selector": "#providerSearch",
                "source_url": "https://example.com/find-care",
                "typed_length": 12,
                "role": "textbox",
                "accessible_name": "Provider name or identifier",
                "trajectory_index": 0,
            }
        ]
        submitted = _yaml(
            """
            title: Directory lookup
            workflow_definition:
              parameters:
              - {key: first_name, default_value: "Given"}
              - {key: last_name, default_value: "Family"}
              blocks:
              - block_type: code
                label: search_directory
                parameter_keys:
                - provider_name_or_identifier
                code: |
                  await page.locator("#providerSearch").fill(str(provider_name_or_identifier))
            """
        )

        result = await _update_workflow({"workflow_yaml": submitted}, ctx)

        assert result["ok"] is True
        parsed = parse_workflow_yaml(ctx.workflow_yaml)
        assert isinstance(parsed, dict)
        block = _single_code_block(parsed)
        assert block["parameter_keys"] == ["first_name", "last_name"]
        assert '(str(first_name) + " " + str(last_name))' in block["code"]
        assert "provider_name_or_identifier" not in block["code"]
        assert result["data"]["imposed_substitutions"]["parameter_expressions"] == {
            "provider_name_or_identifier": ('(str(first_name) + " " + str(last_name))')
        }

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
    async def test_single_submitted_string_parameter_is_adopted_for_synthesized_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_successful_update(monkeypatch)
        ctx = self._provider_search_ctx()
        ctx.scout_trajectory[0]["accessible_name"] = "Search by doctor name or specialty, hospital, procedure, and more"

        result = await _update_workflow({"workflow_yaml": _SUBMITTED_COMPUTED_PARAMETER_YAML}, ctx)

        assert result["ok"] is True
        parsed = parse_workflow_yaml(ctx.workflow_yaml)
        assert isinstance(parsed, dict)
        block = _single_code_block(parsed)
        synthesized_key = "search_by_doctor_name_or_specialty_hospital_procedure_and_more"
        assert f'await page.locator("#provInput").fill(str({synthesized_key}))' in block["code"]
        assert block["parameter_keys"] == [synthesized_key]
        assert parsed["workflow_definition"]["parameters"] == [
            {
                "parameter_type": "workflow",
                "workflow_parameter_type": "string",
                "key": synthesized_key,
                "default_value": "Sample Search",
            }
        ]
        assert result["data"]["imposed_substitutions"]["parameter_keys"] == [synthesized_key]

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

        result = await _update_workflow({"workflow_yaml": self._SUBMIT_CODE_YAML}, ctx)

        assert result["ok"] is False
        assert "fill_credential_field" in result["error"]
        assert "click the submit control or press Enter" in result["error"]
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
    async def test_counter_climbs_on_distinct_rejects_and_resets_on_accept(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _code_only_ctx()
        for index in range(MAX_CODE_AUTHORING_GUARDRAIL_REJECTS - 1):
            result = await _update_workflow({"workflow_yaml": _distinct_guardrail_yaml(index)}, ctx)
            assert result["ok"] is False
            assert ctx.code_authoring_guardrail_reject_count == index + 1
        assert ctx.blocker_signal is None

        _stub_successful_update(monkeypatch)
        accepted = await _update_workflow({"workflow_yaml": _SAFE_CODE_YAML}, ctx)
        assert accepted["ok"] is True
        assert ctx.code_authoring_guardrail_reject_count == 0

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
    async def test_nth_reject_stashes_churn_signal_and_resolves_to_loop_halt(self) -> None:
        ctx = _code_only_ctx()
        for index in range(MAX_CODE_AUTHORING_GUARDRAIL_REJECTS):
            await _update_workflow({"workflow_yaml": _distinct_guardrail_yaml(index)}, ctx)

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

        for index in range(MAX_CODE_AUTHORING_GUARDRAIL_REJECTS):
            await _update_workflow({"workflow_yaml": _distinct_guardrail_yaml(index)}, ctx)

        assert ctx.code_authoring_guardrail_reject_count == MAX_CODE_AUTHORING_GUARDRAIL_REJECTS
        assert ctx.blocker_signal is terminal
        assert ctx.latest_tool_blocker_signal is terminal

    @pytest.mark.asyncio
    async def test_single_credential_priority_reject_defers_to_credential_scout_reply(self) -> None:
        ctx = _code_only_ctx()
        ctx.scout_trajectory = []

        result = await _update_workflow({"workflow_yaml": _distinct_credential_collision_yaml(0)}, ctx)

        assert ctx.code_authoring_guardrail_reject_count == 1
        assert result["ok"] is False
        assert result["data"]["failure_type"] == "missing_credential_or_init"
        assert result["user_facing_summary"] == CREDENTIAL_SCOUT_VERIFY_REPLY
        assert ctx.blocker_signal is None
        assert ctx.latest_tool_blocker_signal is None

    @pytest.mark.asyncio
    async def test_credential_priority_reject_defers_below_higher_bound(self) -> None:
        ctx = _code_only_ctx()
        ctx.scout_trajectory = []

        for index in range(MAX_CREDENTIAL_PRIORITY_AUTHORING_REJECTS - 1):
            await _update_workflow({"workflow_yaml": _distinct_credential_collision_yaml(index)}, ctx)

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
            result = await _update_workflow({"workflow_yaml": _distinct_credential_collision_yaml(index)}, ctx)

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
            await _update_workflow({"workflow_yaml": _distinct_credential_collision_yaml(index)}, ctx)

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
            await _update_workflow({"workflow_yaml": _distinct_credential_collision_yaml(index)}, ctx)
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

        assert ctx.code_authoring_guardrail_reject_count == MAX_CODE_AUTHORING_GUARDRAIL_REJECTS
        assert ctx.last_code_authoring_reject_was_credential_priority is False
        with pytest.raises(CopilotTurnHalt) as excinfo:
            _check_enforcement(ctx)
        assert excinfo.value.halt.kind is TurnHaltKind.LOOP_DETECTED
        churn = ctx.blocker_signal
        assert isinstance(churn, CopilotToolBlockerSignal)
        assert churn.internal_reason_code == "code_authoring_guardrail_churn"

    @pytest.mark.asyncio
    async def test_non_credential_reject_halts_immediately_when_count_pre_charged(self) -> None:
        # Credential-priority rejects can push the shared counter into the 4..8 band where the
        # credential bound still defers; a later non-credential reject flips the flag and halts at
        # once on the non-credential N=4, since the count is already past it.
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

        assert ctx.code_authoring_guardrail_reject_count == MAX_CODE_AUTHORING_GUARDRAIL_REJECTS + 2
        assert ctx.last_code_authoring_reject_was_credential_priority is False
        with pytest.raises(CopilotTurnHalt) as excinfo:
            _check_enforcement(ctx)
        assert excinfo.value.halt.kind is TurnHaltKind.LOOP_DETECTED
        churn = ctx.blocker_signal
        assert isinstance(churn, CopilotToolBlockerSignal)
        assert churn.internal_reason_code == "code_authoring_guardrail_churn"

    @pytest.mark.asyncio
    async def test_accept_after_latched_churn_clears_both_signals(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx = _code_only_ctx()
        for index in range(MAX_CODE_AUTHORING_GUARDRAIL_REJECTS):
            await _update_workflow({"workflow_yaml": _distinct_guardrail_yaml(index)}, ctx)
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
