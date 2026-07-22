"""Tests for the pure copilot code-block synthesizer.

OSS-synced: only example.* / RFC-2606 placeholder targets.
"""

from __future__ import annotations

import ast
import asyncio
import json
import keyword
import sys
import textwrap
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from skyvern.forge.sdk.copilot.authoring_parameter_binding import (
    _SELECTION_MATCH_BASES,
    AuthoringParameterBindingCandidate,
    AuthoringParameterFieldBinding,
    AuthoringParameterTerminalBinding,
    authored_selection_parameter_bindings,
    authored_selector_parameter_bindings,
    authoring_parameter_binding_directive_consumed,
    build_authoring_parameter_binding_directive,
    build_authoring_parameter_binding_snapshot,
)
from skyvern.forge.sdk.copilot.code_block_preflight import preflight_code_block
from skyvern.forge.sdk.copilot.code_block_security import author_time_code_security_errors
from skyvern.forge.sdk.copilot.code_block_synthesis import (
    _DOWNLOAD_VAR_BASE,
    _ENTRY_RESUME_AFTER_AUTH_VAR,
    _ENTRY_REUSED_VAR,
    _ENTRY_TARGET_VAR,
    _INDENT,
    _MAX_STEPS,
    _READONLY_DEFERRED_VAR,
    _SYNTHESIZED_BLOCK_LABEL,
    CREDENTIAL_FILL_TOOL_NAME,
    INPUT_TEMPLATED_PROVENANCE_SOURCE,
    LOCATOR_WITNESS_PARAM_SOURCE,
    SCOUTED_SPINE_DROPPED_UNFORGIVEN_REASON_CODE,
    SCOUTED_SPINE_TRUNCATED_REASON_CODE,
    TRUNCATED_FINDING,
    UNFORGIVEN_DROP_FINDING,
    UNRECORDED_INDEX_FINDING,
    ProducedStaticReturnEnvelope,
    SynthesisDiagnostics,
    _get_by_role_expr,
    _get_by_role_expr_strict,
    _is_submit_interaction,
    build_input_templated_locator,
    build_synthesized_artifact_metadata,
    code_contains_credential_fill,
    credential_scout_gap,
    input_correspondences_for_interaction,
    is_optional_dismissal_only_trajectory,
    obligation_finding_reason_code,
    produce_covered_static_return_envelope,
    render_synthesized_offer_text,
    spine_partition_findings,
    synthesize_code_block,
    synthesize_code_block_with_extraction,
    synthesize_extraction_suffix,
    templated_selection_locator_binding,
    uncovered_required_emitted_interactions,
    witness_prelude_lines,
)
from skyvern.forge.sdk.copilot.context import (
    FillCarry,
    StructuredContext,
    _fill_carry_from_scout_trajectory,
)
from skyvern.forge.sdk.copilot.output_extraction_plan import (
    LiveReadBinding,
    LiveReadKind,
    RequestedOutputExtractionPlan,
    RevealAnchor,
)
from skyvern.forge.sdk.copilot.reached_download_target import ReachedDownloadTarget
from skyvern.forge.sdk.copilot.tools import _normalize_code_artifact_metadata
from skyvern.forge.sdk.copilot.tools.scouting import _fill_carry_to_interaction, _with_trajectory_anchor
from skyvern.forge.sdk.copilot.tools.workflow_update import _code_block_safety_errors
from skyvern.forge.sdk.workflow.models.block import CodeBlock, CodeBlockStep


@pytest.fixture(autouse=True)
def _stub_mypy_for_static_policy_cases(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if "real_mypy" in request.fixturenames:
        return
    fake_mypy = ModuleType("mypy")
    fake_mypy.__dict__["api"] = SimpleNamespace(run=lambda _args: ("", "", 0))
    monkeypatch.setitem(sys.modules, "mypy", fake_mypy)


@pytest.fixture
def real_mypy() -> None:
    return None


def _interaction(tool_name: str, **fields: Any) -> dict[str, Any]:
    return {"tool_name": tool_name, **fields}


def test_authoring_parameter_snapshot_rebinds_captured_fill_without_duplicate() -> None:
    trajectory = [
        _interaction("type_text", selector="#location", source_url="https://example.com/form", trajectory_index=7),
        _interaction("click", selector="#submit", source_url="https://example.com/form", trajectory_index=9),
    ]
    snapshot = build_authoring_parameter_binding_snapshot(
        structural_key="definition-reject",
        source_origin="https://example.com",
        field_bindings=[
            AuthoringParameterFieldBinding(
                declared_key="search_location",
                field_selector="#location",
                field_trajectory_index=7,
                match_basis="unique_ephemeral_value",
            )
        ],
        terminal=AuthoringParameterTerminalBinding(
            tool_name="click",
            trajectory_index=9,
            selector="#submit",
        ),
    )

    result = synthesize_code_block(trajectory, strict_selectors=True, parameter_binding_snapshot=snapshot)

    assert result is not None
    fill = 'page.locator("#location").fill(str(search_location))'
    assert result.code.count(fill) == 1
    assert result.code.index(fill) < result.code.index('page.locator("#submit").click()')
    assert result.parameters == [{"key": "search_location"}]
    assert result.diagnostics.grounded_submit_binding_fingerprints == [snapshot.fingerprint]


def test_authoring_parameter_snapshot_recovers_missing_fill_before_enter() -> None:
    trajectory = [
        _interaction(
            "press_key",
            selector="#location",
            key="Enter",
            source_url="https://example.com/form",
            trajectory_index=0,
        )
    ]
    snapshot = build_authoring_parameter_binding_snapshot(
        structural_key="definition-reject",
        source_origin="https://example.com",
        field_bindings=[
            AuthoringParameterFieldBinding(
                declared_key="search_location",
                field_selector="#location",
                match_basis="unique_ephemeral_value",
            )
        ],
        terminal=AuthoringParameterTerminalBinding(
            tool_name="press_key",
            trajectory_index=0,
            selector="#location",
            key="Enter",
        ),
    )

    result = synthesize_code_block(trajectory, strict_selectors=True, parameter_binding_snapshot=snapshot)

    assert result is not None
    fill = 'page.locator("#location").fill(str(search_location))'
    press = 'page.locator("#location").press("Enter")'
    assert result.code.count(fill) == 1
    assert result.code.index(fill) < result.code.index(press)
    assert result.parameters == [{"key": "search_location"}]


def test_authoring_parameter_directive_consumption_requires_structural_and_final_code_evidence() -> None:
    snapshot = build_authoring_parameter_binding_snapshot(
        structural_key="definition-reject",
        source_origin="https://example.com",
        field_bindings=[
            AuthoringParameterFieldBinding(
                declared_key="search_location",
                field_selector="#location",
                field_trajectory_index=0,
                match_basis="exact_authored_selector",
            )
        ],
        terminal=AuthoringParameterTerminalBinding(
            tool_name="click",
            trajectory_index=1,
            selector="#submit",
        ),
    )
    directive = build_authoring_parameter_binding_directive(
        structural_key="definition-reject",
        source_origin="https://example.com",
        candidates=[
            AuthoringParameterBindingCandidate(
                declared_key="search_location",
                field_selector="#location",
            )
        ],
    )
    code = 'await page.locator("#location").fill(str(search_location))'

    assert authoring_parameter_binding_directive_consumed(
        directive,
        snapshot,
        code=code,
        parameter_keys=["search_location"],
    )
    assert not authoring_parameter_binding_directive_consumed(
        directive.model_copy(update={"structural_key": "stale"}),
        snapshot,
        code=code,
        parameter_keys=["search_location"],
    )
    assert not authoring_parameter_binding_directive_consumed(
        directive,
        snapshot,
        code='await page.locator("#other").fill(str(search_location))',
        parameter_keys=["search_location"],
    )


def test_authoring_parameter_snapshot_fails_closed_when_terminal_identity_changes() -> None:
    trajectory = [_interaction("press_key", selector="#location", key="Tab", source_url="https://example.com/form")]
    snapshot = build_authoring_parameter_binding_snapshot(
        structural_key="definition-reject",
        source_origin="https://example.com",
        field_bindings=[
            AuthoringParameterFieldBinding(
                declared_key="search_location",
                field_selector="#location",
                match_basis="unique_ephemeral_value",
            )
        ],
        terminal=AuthoringParameterTerminalBinding(
            tool_name="press_key",
            trajectory_index=0,
            selector="#location",
            key="Enter",
        ),
    )

    assert synthesize_code_block(trajectory, strict_selectors=True, parameter_binding_snapshot=snapshot) is None


def _templated_selection_click(selector: str, key: str, value: str, index: int) -> dict[str, Any]:
    interaction = _interaction(
        "click", selector=selector, source_url="https://example.com/list", trajectory_index=index
    )
    interaction["input_correspondences"] = input_correspondences_for_interaction(interaction, {key: value})
    return interaction


def test_authored_selection_bindings_recognizes_templated_click_and_select_option() -> None:
    code = (
        'await page.locator(f"[data-account=\\"{account_number}\\"]").click()\n'
        'await page.locator("#plan").select_option(str(plan_tier))\n'
    )
    bindings = authored_selection_parameter_bindings(code, {"account_number", "plan_tier"})
    assert bindings is not None
    assert bindings.get("#plan") == {"plan_tier"}
    assert {key for keys in bindings.values() for key in keys} == {"account_number", "plan_tier"}
    assert authored_selector_parameter_bindings(code, {"account_number", "plan_tier"}) == {}


def test_authored_selection_bindings_ignores_literal_only_click() -> None:
    code = 'await page.locator("#row-account-AC12345").click()\n'
    assert authored_selection_parameter_bindings(code, {"account_number"}) == {}


def test_authoring_parameter_directive_consumed_via_templated_click() -> None:
    click = _templated_selection_click('[data-account="AC12345"]', "account_number", "AC12345", 0)
    key, join = templated_selection_locator_binding(click)
    assert key == "account_number"
    snapshot = build_authoring_parameter_binding_snapshot(
        structural_key="definition-reject",
        source_origin="https://example.com",
        field_bindings=[
            AuthoringParameterFieldBinding(
                declared_key="account_number",
                field_selector=join,
                field_trajectory_index=0,
                match_basis="scouted_selection_value",
            )
        ],
        terminal=AuthoringParameterTerminalBinding(
            tool_name="click", trajectory_index=0, selector='[data-account="AC12345"]'
        ),
    )
    directive = build_authoring_parameter_binding_directive(
        structural_key="definition-reject",
        source_origin="https://example.com",
        candidates=[AuthoringParameterBindingCandidate(declared_key="account_number", field_selector=join)],
    )
    consumed_code = 'await page.locator(f"[data-account=\\"{account_number}\\"]").click()'
    assert authoring_parameter_binding_directive_consumed(
        directive, snapshot, code=consumed_code, parameter_keys=["account_number"]
    )
    assert not authoring_parameter_binding_directive_consumed(
        directive,
        snapshot,
        code='await page.locator("#row-account-AC12345").click()',
        parameter_keys=["account_number"],
    )


def test_authoring_parameter_directive_consumed_via_select_option_value_argument() -> None:
    snapshot = build_authoring_parameter_binding_snapshot(
        structural_key="definition-reject",
        source_origin="https://example.com",
        field_bindings=[
            AuthoringParameterFieldBinding(
                declared_key="plan_tier",
                field_selector="#plan",
                field_trajectory_index=0,
                match_basis="scouted_option_value",
            )
        ],
        terminal=AuthoringParameterTerminalBinding(tool_name="select_option", trajectory_index=0, selector="#plan"),
    )
    directive = build_authoring_parameter_binding_directive(
        structural_key="definition-reject",
        source_origin="https://example.com",
        candidates=[AuthoringParameterBindingCandidate(declared_key="plan_tier", field_selector="#plan")],
    )
    assert authoring_parameter_binding_directive_consumed(
        directive,
        snapshot,
        code='await page.locator("#plan").select_option(str(plan_tier))',
        parameter_keys=["plan_tier"],
    )
    assert not authoring_parameter_binding_directive_consumed(
        directive,
        snapshot,
        code='await page.locator("#plan").select_option("premium")',
        parameter_keys=["plan_tier"],
    )


def test_selection_snapshot_click_binding_references_declared_key() -> None:
    click = _templated_selection_click('[data-account="AC12345"]', "account_number", "AC12345", 0)
    _key, join = templated_selection_locator_binding(click)
    snapshot = build_authoring_parameter_binding_snapshot(
        structural_key="definition-reject",
        source_origin="https://example.com",
        field_bindings=[
            AuthoringParameterFieldBinding(
                declared_key="account_number",
                field_selector=join,
                field_trajectory_index=0,
                match_basis="scouted_selection_value",
            )
        ],
        terminal=AuthoringParameterTerminalBinding(
            tool_name="click", trajectory_index=0, selector='[data-account="AC12345"]'
        ),
    )
    result = synthesize_code_block([click], strict_selectors=True, parameter_binding_snapshot=snapshot)
    assert result is not None
    assert 'page.locator(f"[data-account=\\"{account_number}\\"]").click()' in result.code
    assert ".fill(" not in result.code


def test_selection_snapshot_select_option_binds_value_argument() -> None:
    select = _interaction(
        "select_option", selector="#plan", value="premium", source_url="https://example.com/list", trajectory_index=0
    )
    snapshot = build_authoring_parameter_binding_snapshot(
        structural_key="definition-reject",
        source_origin="https://example.com",
        field_bindings=[
            AuthoringParameterFieldBinding(
                declared_key="plan_tier",
                field_selector="#plan",
                field_trajectory_index=0,
                match_basis="scouted_option_value",
            )
        ],
        terminal=AuthoringParameterTerminalBinding(tool_name="select_option", trajectory_index=0, selector="#plan"),
    )
    result = synthesize_code_block([select], strict_selectors=True, parameter_binding_snapshot=snapshot)
    assert result is not None
    assert 'page.locator("#plan").select_option(str(plan_tier))' in result.code
    assert '"premium"' not in result.code
    assert result.parameters == [{"key": "plan_tier"}]


def test_fill_snapshot_never_emits_select_option_value_binding() -> None:
    trajectory = [
        _interaction("type_text", selector="#location", source_url="https://example.com/form", trajectory_index=7),
        _interaction("click", selector="#submit", source_url="https://example.com/form", trajectory_index=9),
    ]
    snapshot = build_authoring_parameter_binding_snapshot(
        structural_key="definition-reject",
        source_origin="https://example.com",
        field_bindings=[
            AuthoringParameterFieldBinding(
                declared_key="search_location",
                field_selector="#location",
                field_trajectory_index=7,
                match_basis="unique_ephemeral_value",
            )
        ],
        terminal=AuthoringParameterTerminalBinding(tool_name="click", trajectory_index=9, selector="#submit"),
    )
    result = synthesize_code_block(trajectory, strict_selectors=True, parameter_binding_snapshot=snapshot)
    assert result is not None
    assert ".select_option(" not in result.code
    assert snapshot.terminal.tool_name not in _SELECTION_MATCH_BASES


def test_rekeyed_outputs_with_alike_labels_return_under_distinct_keys() -> None:
    # Labels are not guaranteed distinct, so alike-slugging labels must not collapse onto one key.
    plan = RequestedOutputExtractionPlan(
        requested_output_paths=("output.request_slot_aaa_00", "output.request_slot_aaa_01"),
        observation_step=3,
        observation_identity="observation-identity",
        reveal=RevealAnchor(selector="#show"),
        live_reads=(
            LiveReadBinding(
                output_path="output.request_slot_aaa_00",
                kind=LiveReadKind.KEY_VALUE,
                selector=".kv",
                selector_count=2,
                selector_index=0,
                child_index=1,
                child_count=2,
                relation_label="Visitors",
            ),
            LiveReadBinding(
                output_path="output.request_slot_aaa_01",
                kind=LiveReadKind.KEY_VALUE,
                selector=".kv",
                selector_count=2,
                selector_index=1,
                child_index=1,
                child_count=2,
                relation_label="visitors",
            ),
        ),
        identity="plan-identity",
    )

    suffix = synthesize_extraction_suffix(plan)

    assert suffix is not None
    assert '"visitors": _extraction_value_0' in suffix.code
    assert '"visitors_2": _extraction_value_1' in suffix.code
    assert "request_slot_aaa" not in suffix.code


def _extraction_plan() -> RequestedOutputExtractionPlan:
    return RequestedOutputExtractionPlan(
        requested_output_paths=(
            "output.records[].detail",
            "output.records[].state",
            "output.record_id",
            "output.overall_state",
        ),
        observation_step=9,
        observation_identity="observation-identity",
        reveal=RevealAnchor(selector="#show-details"),
        live_reads=(
            LiveReadBinding(
                output_path="output.record_id",
                kind=LiveReadKind.KEY_VALUE,
                selector=".kv",
                selector_count=2,
                selector_index=0,
                child_index=1,
                child_count=2,
                relation_label="Record Identifier",
            ),
            LiveReadBinding(
                output_path="output.records[].detail",
                kind=LiveReadKind.TABLE_COLUMN,
                selector="#records",
                selector_count=1,
                selector_index=0,
                row_selector="#records > tbody > tr",
                row_count=3,
                column_index=1,
                headers=("Record", "Detail", "State"),
                row_cell_counts=(3, 3, 3),
                row_identities=("One Detail State", "Two Detail State", "Three Detail State"),
            ),
            LiveReadBinding(
                output_path="output.records[].state",
                kind=LiveReadKind.TABLE_COLUMN,
                selector="#records",
                selector_count=1,
                selector_index=0,
                row_selector="#records > tbody > tr",
                row_count=3,
                column_index=2,
                headers=("Record", "Detail", "State"),
                row_cell_counts=(3, 3, 3),
                row_identities=("One Detail State", "Two Detail State", "Three Detail State"),
            ),
            LiveReadBinding(
                output_path="output.overall_state",
                kind=LiveReadKind.KEY_VALUE,
                selector=".kv",
                selector_count=2,
                selector_index=1,
                child_index=1,
                child_count=2,
                relation_label="Overall State",
            ),
        ),
        identity="plan-identity",
    )


def test_extraction_suffix_compiles_direct_guarded_live_reads() -> None:
    suffix = synthesize_extraction_suffix(_extraction_plan())

    assert suffix is not None
    assert 'page.locator(".kv").nth(0).locator(":scope > *").nth(1).inner_text()' in suffix.code
    assert 'page.locator("#records > tbody > tr").count() != 3' in suffix.code
    assert "for _extraction_index" not in suffix.code
    assert 'page.locator("#records > tbody > tr").nth(2)' in suffix.code
    assert '"overall_state": _extraction_value_1' in suffix.code
    assert '"detail"' in suffix.code
    assert '"state"' in suffix.code


class _RecipeLocator:
    def __init__(self, page: _RecipePage, selector: str, indices: tuple[int, ...] = ()) -> None:
        self.page = page
        self.selector = selector
        self.indices = indices

    def nth(self, index: int) -> _RecipeLocator:
        return _RecipeLocator(self.page, self.selector, (*self.indices, index))

    def locator(self, selector: str) -> _RecipeLocator:
        return _RecipeLocator(self.page, f"{self.selector}|{selector}", self.indices)

    async def count(self) -> int:
        return self.page.counts[(self.selector, self.indices)]

    async def is_visible(self) -> bool:
        return self.page.visibility.get((self.selector, self.indices), True)

    async def inner_text(self) -> str:
        return self.page.text[(self.selector, self.indices)]


class _RecipePage:
    def __init__(self) -> None:
        self.visibility: dict[tuple[str, tuple[int, ...]], bool] = {}
        self.counts: dict[tuple[str, tuple[int, ...]], int] = {
            (".kv", ()): 2,
            (".kv|:scope > *", (0,)): 2,
            (".kv|:scope > *", (1,)): 2,
            ("#records", ()): 1,
            ("#records|:scope > thead > tr > th", (0,)): 3,
            ("#records|[colspan], [rowspan]", (0,)): 0,
            ("#records > tbody > tr", ()): 3,
            ("#records|:scope table", (0,)): 0,
        }
        self.text: dict[tuple[str, tuple[int, ...]], str] = {
            (".kv|:scope > *", (0, 0)): "Record Identifier",
            (".kv|:scope > *", (0, 1)): "record-123",
            (".kv|:scope > *", (1, 0)): "Overall State",
            (".kv|:scope > *", (1, 1)): "Ready",
        }
        for index, header in enumerate(("Record", "Detail", "State")):
            self.text[("#records|:scope > thead > tr > th", (0, index))] = header
        for row, identity in enumerate(("One Detail State", "Two Detail State", "Three Detail State")):
            self.counts[("#records > tbody > tr|:scope > th, :scope > td", (row,))] = 3
            self.counts[("#records > tbody > tr|:scope > th", (row,))] = 0
            self.text[("#records > tbody > tr", (row,))] = identity
            self.text[("#records > tbody > tr|:scope > th, :scope > td", (row, 1))] = f"Detail {row}"
            self.text[("#records > tbody > tr|:scope > th, :scope > td", (row, 2))] = "Ready"

    def locator(self, selector: str) -> _RecipeLocator:
        return _RecipeLocator(self, selector)


async def _execute_recipe(page: _RecipePage) -> dict[str, object]:
    suffix = synthesize_extraction_suffix(_extraction_plan())
    assert suffix is not None
    namespace: dict[str, object] = {}
    exec("async def recipe(page):\n" + textwrap.indent(suffix.code, "    "), namespace)
    recipe = namespace["recipe"]
    assert callable(recipe)
    return await recipe(page)


@pytest.mark.asyncio
async def test_generated_recipe_executes_and_fails_closed_on_runtime_drift() -> None:
    page = _RecipePage()
    result = await _execute_recipe(page)
    assert result["output"]["record_id"] == "record-123"
    assert len(result["output"]["records"]) == 3

    page.counts[(".kv", ())] = 3
    with pytest.raises(ValueError, match="scalar selector cardinality"):
        await _execute_recipe(page)

    page.counts[(".kv", ())] = 2
    page.visibility[("#records > tbody > tr|:scope > th, :scope > td", (1, 2))] = False
    with pytest.raises(ValueError, match="cell is no longer visible"):
        await _execute_recipe(page)


def _produce_table_envelope() -> ProducedStaticReturnEnvelope | None:
    return produce_covered_static_return_envelope(
        "x = 1",
        plan=_extraction_plan(),
        scalar_required_paths=set(_extraction_plan().requested_output_paths),
        declaration_paths=set(),
        download_required_paths=set(),
        expects_download=False,
    )


async def _execute_envelope(page: _RecipePage, envelope: ProducedStaticReturnEnvelope | None) -> dict[str, object]:
    assert envelope is not None
    namespace: dict[str, object] = {}
    exec("async def recipe(page):\n" + textwrap.indent(envelope.code, "    "), namespace)
    recipe = namespace["recipe"]
    assert callable(recipe)
    return await recipe(page)


@pytest.mark.asyncio
async def test_produced_envelope_executes_table_and_scalar_reads() -> None:
    result = await _execute_envelope(_RecipePage(), _produce_table_envelope())
    assert result["output"]["record_id"] == "record-123"
    assert result["output"]["overall_state"] == "Ready"
    assert len(result["output"]["records"]) == 3
    assert result["output"]["records"][0]["detail"] == "Detail 0"
    assert result["output"]["records"][2]["state"] == "Ready"


@pytest.mark.asyncio
async def test_produced_envelope_guard_raises_on_empty_cell() -> None:
    page = _RecipePage()
    page.text[("#records > tbody > tr|:scope > th, :scope > td", (0, 1))] = ""
    with pytest.raises(ValueError, match="table cell value is empty"):
        await _execute_envelope(page, _produce_table_envelope())


def test_suffix_omits_empty_cell_guard() -> None:
    suffix = synthesize_extraction_suffix(_extraction_plan())
    assert suffix is not None
    assert "table cell value is empty" not in suffix.code
    assert "scalar value is empty" not in suffix.code


def test_plan_compiler_requires_exact_reveal_and_is_idempotent() -> None:
    trajectory = [
        _interaction(
            "click",
            selector="#show-details",
            role="button",
            accessible_name="Show details",
            source_url="https://example.com/records",
        )
    ]

    first = synthesize_code_block_with_extraction(trajectory, _extraction_plan())
    second = synthesize_code_block_with_extraction(trajectory, _extraction_plan())

    assert first is not None
    assert second is not None
    assert first.code == second.code
    assert first.code.count(".click()") == 1
    assert first.extraction_plan_identity == "plan-identity"
    assert first.extraction_fingerprint == second.extraction_fingerprint
    assert synthesize_code_block_with_extraction([], _extraction_plan()) is None


class TestLocatorSynthesis:
    def test_role_selector_emits_get_by_role(self) -> None:
        # A `role=...` selector is an ARIA anchor (ref_to_selector form), not a native CSS path.
        result = synthesize_code_block(
            [
                _interaction(
                    "click",
                    selector='role=button[name="Add to cart"]',
                    source_url="https://example.com/product",
                )
            ]
        )
        assert result is not None
        assert 'await page.get_by_role("button", name="Add to cart").click()' in result.code
        assert "get_by_role" in result.code

    def test_stable_id_selector_is_emitted_verbatim_not_get_by_role(self) -> None:
        # Selector-first: a captured stable selector (id) wins over a get_by_role anchor, because the
        # scout's a11y-name read may not reproduce on the raw page the code block runs against.
        result = synthesize_code_block(
            [
                _interaction(
                    "type_text",
                    selector="#search-box",
                    source_url="https://example.com/",
                    typed_length=11,
                    role="textbox",
                    accessible_name="Search",
                )
            ]
        )
        assert result is not None
        assert 'await page.locator("#search-box").fill(str(search))' in result.code
        assert "get_by_role" not in result.code

    def test_stable_attribute_selector_is_emitted_verbatim(self) -> None:
        # [name=...] / [data-testid=...] etc. are stable identity anchors — kept verbatim.
        result = synthesize_code_block(
            [
                _interaction(
                    "click",
                    selector='[data-testid="add-to-cart"]',
                    source_url="https://example.com/product",
                    role="button",
                    accessible_name="Add to cart",
                )
            ]
        )
        assert result is not None
        assert 'await page.locator("[data-testid=\\"add-to-cart\\"]").click()' in result.code
        assert "get_by_role" not in result.code

    def test_positional_nth_of_type_selector_uses_get_by_role_fallback(self) -> None:
        # A positional CSS path is fragile; when a role/name anchor exists, prefer it.
        result = synthesize_code_block(
            [
                _interaction(
                    "click",
                    selector="div.list > button:nth-of-type(3)",
                    source_url="https://example.com/list",
                    role="button",
                    accessible_name="More",
                )
            ]
        )
        assert result is not None
        assert 'await page.get_by_role("button", name="More").click()' in result.code

    def test_nth_engine_chain_uses_get_by_role_anchor(self) -> None:
        result = synthesize_code_block(
            [
                _interaction(
                    "click",
                    selector='role=button[name="More"] >> nth=2',
                    source_url="https://example.com/list",
                    role="button",
                    accessible_name="More",
                )
            ]
        )
        assert result is not None
        assert 'await page.get_by_role("button", name="More").click()' in result.code

    def test_positional_selector_without_role_name_keeps_selector_with_note(self) -> None:
        result = synthesize_code_block(
            [_interaction("click", selector="ul > li:nth-child(2)", source_url="https://example.com/results")]
        )
        assert result is not None
        assert 'await page.locator("ul > li:nth-child(2)").click()' in result.code
        assert any("low-confidence" in note for note in result.notes)

    def test_stable_bare_css_without_role_name_is_emitted_verbatim_no_note(self) -> None:
        result = synthesize_code_block(
            [_interaction("click", selector=".results .item", source_url="https://example.com/results")]
        )
        assert result is not None
        assert 'await page.locator(".results .item").click()' in result.code
        assert not any("low-confidence" in note for note in result.notes)

    def test_positional_role_name_anchor_does_not_emit_first(self) -> None:
        result = synthesize_code_block(
            [
                _interaction(
                    "click",
                    selector='role=link[name="Next"] >> nth=0',
                    source_url="https://example.com/",
                )
            ]
        )
        assert result is not None
        assert ".first" not in result.code
        assert ".last" not in result.code

    def test_bare_tag_selector_disambiguated_to_first(self) -> None:
        result = synthesize_code_block(
            [_interaction("click", selector="button", source_url="https://example.com/login")]
        )
        assert result is not None
        assert 'await page.locator("button").first.click()' in result.code
        assert any("disambiguated a bare" in note for note in result.notes)
        assert any(p.get("source") == "first_fallback" for p in result.diagnostics.locator_provenance)

    def test_bare_role_no_name_disambiguated_to_first(self) -> None:
        result = synthesize_code_block(
            [_interaction("click", selector="role=button", source_url="https://example.com/login")]
        )
        assert result is not None
        assert 'await page.get_by_role("button").first.click()' in result.code

    def test_bare_selector_with_role_name_anchors_on_get_by_role(self) -> None:
        result = synthesize_code_block(
            [
                _interaction(
                    "click",
                    selector="button",
                    source_url="https://example.com/login",
                    role="button",
                    accessible_name="Continue",
                )
            ]
        )
        assert result is not None
        assert 'await page.get_by_role("button", name="Continue").click()' in result.code
        assert ".first" not in result.code

    def test_stable_selector_not_disambiguated_to_first(self) -> None:
        for selector in ("#submit", '[name="email"]', '[data-testid="go"]', ".results .item"):
            result = synthesize_code_block(
                [_interaction("click", selector=selector, source_url="https://example.com/")]
            )
            assert result is not None
            assert ".first" not in result.code, selector

    def test_strict_imposed_refuses_ambiguous_bare_selector(self) -> None:
        trajectory = [
            _interaction("click", selector="#open-login", source_url="https://example.com/home"),
            _interaction("click", selector="button", source_url="https://example.com/login"),
        ]
        result = synthesize_code_block(trajectory, strict_selectors=True)
        assert result is not None
        assert ".first" not in result.code
        assert 'await page.locator("button").click()' not in result.code
        dropped = [
            d for d in result.diagnostics.dropped_interactions if d.get("reason_code") == "ambiguous_bare_selector"
        ]
        assert dropped

    def test_attribute_selector_emitted_verbatim_when_not_scout_ambiguous(self) -> None:
        trajectory = [
            _interaction("click", selector="#open", source_url="https://example.com/home"),
            _interaction(
                "click",
                selector="button[data-action='orderDocuments']",
                source_url="https://example.com/portal",
                role="button",
                accessible_name="Order Documents",
            ),
        ]
        result = synthesize_code_block(trajectory, strict_selectors=True)
        assert result is not None
        assert "button[data-action='orderDocuments']" in result.code

    def test_strict_reanchors_scout_ambiguous_attribute_selector(self) -> None:
        trajectory = [
            _interaction("click", selector="#open", source_url="https://example.com/home"),
            _interaction(
                "click",
                selector="button[data-action='orderDocuments']",
                source_url="https://example.com/portal",
                role="button",
                accessible_name="Order Documents",
                ambiguous=True,
            ),
        ]
        result = synthesize_code_block(trajectory, strict_selectors=True)
        assert result is not None
        assert "data-action" not in result.code
        assert 'await page.get_by_role("button", name="Order Documents", exact=True).click()' in result.code

    def test_strict_drops_scout_ambiguous_attribute_selector_without_role_name(self) -> None:
        trajectory = [
            _interaction("click", selector="#open", source_url="https://example.com/home"),
            _interaction(
                "click",
                selector="button[data-action='businessToggle']",
                source_url="https://example.com/portal",
                ambiguous=True,
            ),
        ]
        result = synthesize_code_block(trajectory, strict_selectors=True)
        assert result is not None
        assert "data-action" not in result.code
        dropped = [
            d for d in result.diagnostics.dropped_interactions if d.get("reason_code") == "ambiguous_bare_selector"
        ]
        assert dropped

    def test_strict_imposed_refuses_bare_role_no_name(self) -> None:
        trajectory = [
            _interaction("click", selector="#open-login", source_url="https://example.com/home"),
            _interaction("click", selector="role=button", source_url="https://example.com/login"),
        ]
        result = synthesize_code_block(trajectory, strict_selectors=True)
        assert result is not None
        assert ".first" not in result.code
        dropped = [
            d for d in result.diagnostics.dropped_interactions if d.get("reason_code") == "ambiguous_bare_selector"
        ]
        assert dropped

    def test_two_bare_button_login_first_clicks_both_emit_first(self) -> None:
        trajectory = [
            _interaction("click", selector="button", source_url="https://example.com/login"),
            _interaction("click", selector="button", source_url="https://example.com/login"),
        ]
        result = synthesize_code_block(trajectory)
        assert result is not None
        assert result.code.count('await page.locator("button").first.click()') == 2
        ast.parse("async def _block(page):\n" + result.code)

    def test_universal_selector_offered_with_first(self) -> None:
        result = synthesize_code_block([_interaction("click", selector="*", source_url="https://example.com/p")])
        assert result is not None
        assert 'await page.locator("*").first.click()' in result.code
        assert 'await page.locator("*").click()' not in result.code

    def test_strict_imposed_refuses_universal_selector(self) -> None:
        trajectory = [
            _interaction("click", selector="#open", source_url="https://example.com/home"),
            _interaction("click", selector="*", source_url="https://example.com/p"),
        ]
        result = synthesize_code_block(trajectory, strict_selectors=True)
        assert result is not None
        assert 'page.locator("*")' not in result.code
        assert [d for d in result.diagnostics.dropped_interactions if d.get("reason_code") == "ambiguous_bare_selector"]

    def test_attribute_qualified_universal_selector_not_disambiguated(self) -> None:
        result = synthesize_code_block(
            [_interaction("click", selector="*[data-id]", source_url="https://example.com/p")]
        )
        assert result is not None
        assert 'await page.locator("*[data-id]").click()' in result.code
        assert ".first" not in result.code

    def test_strict_bare_selector_with_role_name_reanchors_to_get_by_role(self) -> None:
        trajectory = [
            _interaction("click", selector="#statement-row", source_url="https://example.com/billing"),
            _interaction(
                "click",
                selector="a",
                source_url="https://example.com/billing",
                role="link",
                accessible_name="View Statements",
            ),
        ]
        result = synthesize_code_block(trajectory, strict_selectors=True)
        assert result is not None
        assert 'await page.get_by_role("link", name="View Statements", exact=True).click()' in result.code
        assert ".first" not in result.code
        assert result.diagnostics.dropped_interactions == []
        provenance = [p for p in result.diagnostics.locator_provenance if p.get("source") == "aria_role_name"]
        assert provenance == [
            {
                "trajectory_index": 1,
                "selector": "a",
                "emitted_literal": _get_by_role_expr_strict("link", "View Statements"),
                "source": "aria_role_name",
                "role": "link",
                "name": "View Statements",
            }
        ]

    def test_strict_bare_role_selector_with_name_reanchors_to_get_by_role(self) -> None:
        trajectory = [
            _interaction("click", selector="#open", source_url="https://example.com/home"),
            _interaction(
                "click",
                selector="role=link",
                source_url="https://example.com/home",
                role="link",
                accessible_name="Continue",
            ),
        ]
        result = synthesize_code_block(trajectory, strict_selectors=True)
        assert result is not None
        assert 'await page.get_by_role("link", name="Continue", exact=True).click()' in result.code
        assert result.diagnostics.dropped_interactions == []

    def test_strict_bare_selector_without_role_name_is_still_dropped(self) -> None:
        trajectory = [
            _interaction("click", selector="#open-login", source_url="https://example.com/home"),
            _interaction("click", selector="a", source_url="https://example.com/account"),
        ]
        result = synthesize_code_block(trajectory, strict_selectors=True)
        assert result is not None
        assert "get_by_role" not in result.code
        dropped = [
            d for d in result.diagnostics.dropped_interactions if d.get("reason_code") == "ambiguous_bare_selector"
        ]
        assert dropped

    def test_strict_reanchor_escapes_quotes_and_newlines_in_name(self) -> None:
        trajectory = [
            _interaction("click", selector="#open", source_url="https://example.com/home"),
            _interaction(
                "click",
                selector="a",
                source_url="https://example.com/home",
                role="link",
                accessible_name='Say "hi"\nplease',
            ),
        ]
        result = synthesize_code_block(trajectory, strict_selectors=True)
        assert result is not None
        assert result.diagnostics.dropped_interactions == []
        emitted = _get_by_role_expr_strict("link", 'Say "hi"\nplease')
        assert "exact=True" in emitted
        assert "\n" not in emitted
        assert f"await {emitted}.click()" in result.code

    def test_strict_reanchor_emits_exact_name_match_for_repeated_affordance(self) -> None:
        # AC1: a re-anchored named get_by_role on a page with a repeated accessible name must emit an
        # exact (single (role, name) group) match, never the substring default that over-matches.
        trajectory = [
            _interaction("click", selector="#open", source_url="https://example.com/billing"),
            _interaction(
                "click",
                selector="a",
                source_url="https://example.com/billing",
                role="link",
                accessible_name="Download",
            ),
        ]
        result = synthesize_code_block(trajectory, strict_selectors=True)
        assert result is not None
        assert 'await page.get_by_role("link", name="Download", exact=True).click()' in result.code
        assert ".nth(" not in result.code
        assert result.diagnostics.dropped_interactions == []
        provenance = [p for p in result.diagnostics.locator_provenance if p.get("source") == "aria_role_name"]
        assert provenance == [
            {
                "trajectory_index": 1,
                "selector": "a",
                "emitted_literal": _get_by_role_expr_strict("link", "Download"),
                "source": "aria_role_name",
                "role": "link",
                "name": "Download",
            }
        ]
        assert provenance[0]["emitted_literal"] != _get_by_role_expr("link", "Download")


class TestActionSynthesis:
    def test_type_text_becomes_param_slot_fill(self) -> None:
        result = synthesize_code_block(
            [
                _interaction(
                    "type_text",
                    selector='role=textbox[name="Search"]',
                    source_url="https://example.com/",
                    typed_length=11,
                    role="textbox",
                    accessible_name="Search",
                )
            ]
        )
        assert result is not None
        assert 'await page.get_by_role("textbox", name="Search").fill(str(search))' in result.code
        assert result.parameters == [{"key": "search"}]
        # Raw typed value is never captured.
        assert "value" not in result.code

    def test_strict_type_text_carries_typed_length_without_value(self) -> None:
        result = synthesize_code_block(
            [
                _interaction(
                    "type_text",
                    selector="#locInput",
                    source_url="https://example.com/",
                    typed_length=19,
                    role="textbox",
                    accessible_name="Address or postal code",
                )
            ],
            strict_selectors=True,
        )

        assert result is not None
        assert result.parameters == [{"key": "address_or_postal_code", "typed_length": "19"}]
        assert "Example City" not in result.code

    def test_type_text_defaults_are_private_reused_only_for_same_field_identity(self) -> None:
        def typed(selector: str, name: str, url: str = "https://example.com/") -> dict[str, Any]:
            return _interaction(
                "type_text",
                selector=selector,
                source_url=url,
                typed_length=15,
                typed_value="example_sku_123",
                role="textbox",
                accessible_name=name,
            )

        safe = synthesize_code_block([typed('role=textbox[name="Search"]', "Search")])
        assert safe is not None
        assert 'await page.get_by_role("textbox", name="Search").fill(str(search))' in safe.code
        assert "example_sku_123" not in safe.code
        assert safe.parameters == [{"key": "search", "default_value": "example_sku_123"}]

        offer_text = render_synthesized_offer_text(safe)
        assert "workflow_parameter_type: string" in offer_text
        assert "default_value" in offer_text
        assert "example_sku_123" not in offer_text

        reused = synthesize_code_block(
            [typed("#search", "Search"), typed("#search", "Search", "https://example.com/results")]
        )
        assert reused is not None
        assert reused.parameters == [{"key": "search", "default_value": "example_sku_123"}]
        assert reused.code.count("fill(str(search))") == 2

        distinct = synthesize_code_block([typed("#part-number", "Part Number"), typed("#coupon", "Coupon Code")])
        assert distinct is not None
        assert distinct.parameters == [
            {"key": "part_number", "default_value": "example_sku_123"},
            {"key": "coupon_code", "default_value": "example_sku_123"},
        ]

    def test_entry_url_is_selector_gated_and_uses_domcontentloaded(self) -> None:
        result = synthesize_code_block([_interaction("click", selector="#go", source_url="https://example.com/start")])
        assert result is not None
        lines = result.code.splitlines()
        assert lines[0] == '    _scout_entry_target = page.locator("#go")'
        assert lines[1] == "    try:"
        assert lines[2] == '        await _scout_entry_target.wait_for(state="visible", timeout=1000)'
        assert lines[3] == "    except Exception:"
        assert lines[4] == '        await page.goto("https://example.com/start", wait_until="domcontentloaded")'
        assert lines[5] == '        await _scout_entry_target.wait_for(state="visible")'
        assert "        del _scout_entry_target" in lines

    def test_optional_cookie_dismissal_is_conditional_and_uses_durable_entry_target(self) -> None:
        result = synthesize_code_block(
            [
                _interaction(
                    "click",
                    selector="#accept-consent",
                    source_url="https://example.com/find",
                    role="button",
                    accessible_name="Accept cookies",
                ),
                _interaction(
                    "type_text",
                    selector="#locInput",
                    source_url="https://example.com/find",
                    role="textbox",
                    accessible_name="City, county, or ZIP code",
                    typed_value="Example City",
                ),
            ]
        )
        assert result is not None
        lines = result.code.splitlines()
        assert lines[0] == '    _scout_entry_target = page.locator("#locInput")'
        assert '        await page.goto("https://example.com/find", wait_until="domcontentloaded")' in lines
        assert '        await _scout_entry_target.wait_for(state="visible")' in lines
        assert '    _scout_optional_dismissal = page.locator("#accept-consent")' in lines
        assert "    if await _scout_optional_dismissal.count() > 0:" in lines
        assert "            await _scout_optional_dismissal.first.click(timeout=1000)" in lines
        assert result.code.index("_scout_optional_dismissal") < result.code.index(
            'await page.locator("#locInput").fill'
        )
        assert "        del _scout_entry_target" in lines
        assert "        del _scout_optional_dismissal" in lines
        assert result.parameters == [{"key": "city_county_or_zip_code", "default_value": "Example City"}]
        ast.parse("async def _block(page):\n" + result.code)

    def test_optional_cookie_decline_is_conditional_and_not_entry_target(self) -> None:
        result = synthesize_code_block(
            [
                _interaction(
                    "click",
                    selector="button.decline",
                    source_url="https://example.com/find",
                    role="button",
                    accessible_name="Decline cookies",
                ),
                _interaction(
                    "type_text",
                    selector="#locInput",
                    source_url="https://example.com/find",
                    role="textbox",
                    accessible_name="City, county, or ZIP code",
                    typed_value="Example City",
                ),
            ],
            strict_selectors=True,
        )

        assert result is not None
        lines = result.code.splitlines()
        assert lines[0] == '    _scout_entry_target = page.locator("#locInput")'
        assert '    _scout_optional_dismissal = page.locator("button.decline")' in lines
        assert "    if await _scout_optional_dismissal.count() > 0:" in lines
        assert 'await _scout_entry_target.wait_for(state="visible")' in result.code
        assert 'await page.locator("button.decline").click()' not in result.code

    def test_close_named_action_is_not_optional_dismissal_by_name_only(self) -> None:
        trajectory = [
            _interaction(
                "click",
                selector="#account-action",
                source_url="https://example.com/settings",
                role="button",
                accessible_name="Close account",
            )
        ]

        assert is_optional_dismissal_only_trajectory(trajectory) is False

        result = synthesize_code_block(trajectory, strict_selectors=True)

        assert result is not None
        assert 'await page.locator("#account-action").click()' in result.code
        assert "_scout_optional_dismissal" not in result.code

    def test_internal_scout_cleanup_ignores_names_inside_literals(self) -> None:
        result = synthesize_code_block(
            [
                _interaction(
                    "click",
                    selector="#start",
                    source_url="https://example.com/_scout_optional_dismissal",
                )
            ]
        )

        assert result is not None
        assert 'await page.goto("https://example.com/_scout_optional_dismissal"' in result.code
        assert "        del _scout_entry_target" in result.code
        assert "        del _scout_optional_dismissal" not in result.code

    @pytest.mark.parametrize(
        (
            "click_selector",
            "click_role",
            "fill_selector",
            "fill_name",
            "fill_value",
            "dismissal_line",
            "forbidden_snippet",
        ),
        [
            pytest.param(
                ".btns button:nth-of-type(2)",
                "button",
                "#npiInput",
                "Provider ID",
                "ID-12345",
                '    _scout_optional_dismissal = page.locator(".btns button:nth-of-type(2)")',
                'await page.locator(".btns button:nth-of-type(2)").click()',
                id="structural-nth-of-type",
            ),
            pytest.param(
                "button:not(.decline):nth-of-type(6)",
                "button",
                "#locInput",
                "City, county, or ZIP code",
                "Example City",
                "    _scout_optional_dismissal = page.locator(\"button:has-text('Accept')\")",
                'await page.locator("button:not(.decline):nth-of-type(6)").click()',
                id="not-decline-nth-of-type",
            ),
            pytest.param(
                'xpath=/*[name()="html"][1]/*[name()="body"][1]/*[name()="div"][1]'
                '/*[name()="div"][2]/*[name()="div"][1]/*[name()="button"][2]',
                "button",
                "#locInput",
                "City, county, or ZIP code",
                "Example City",
                "    _scout_optional_dismissal = page.locator(\"button:has-text('Accept')\")",
                'xpath=/*[name()="html"][1]/*[name()="body"][1]/*[name()="div"][1]'
                '/*[name()="div"][2]/*[name()="div"][1]/*[name()="button"][2]',
                id="positional-xpath",
            ),
            pytest.param(
                "xpath=//button[normalize-space()='Accept']",
                "button",
                "#locInput",
                "City, county, or ZIP code",
                "Example City",
                "    _scout_optional_dismissal = page.locator(\"button:has-text('Accept')\")",
                "xpath=//button[normalize-space()='Accept']",
                id="normalized-space-xpath",
            ),
            pytest.param(
                "//button[normalize-space()='Accept']",
                None,
                "#locInput",
                "City, county, or ZIP code",
                "Example City",
                "    _scout_optional_dismissal = page.locator(\"button:has-text('Accept')\")",
                "//button[normalize-space()='Accept']",
                id="bare-normalized-space-xpath",
            ),
        ],
    )
    def test_optional_dismissal_is_conditional_when_durable_target_follows(
        self,
        click_selector: str,
        click_role: str | None,
        fill_selector: str,
        fill_name: str,
        fill_value: str,
        dismissal_line: str,
        forbidden_snippet: str,
    ) -> None:
        result = synthesize_code_block(
            [
                _interaction(
                    "click",
                    selector=click_selector,
                    source_url="https://example.com/find",
                    role=click_role,
                ),
                _interaction(
                    "type_text",
                    selector=fill_selector,
                    source_url="https://example.com/find",
                    role="textbox",
                    accessible_name=fill_name,
                    typed_value=fill_value,
                ),
            ],
            strict_selectors=True,
        )

        assert result is not None
        lines = result.code.splitlines()
        assert lines[0] == f'    _scout_entry_target = page.locator("{fill_selector}")'
        assert dismissal_line in lines
        assert "            await _scout_optional_dismissal.first.click(timeout=1000)" in lines
        assert forbidden_snippet not in result.code
        assert result.code.index("_scout_optional_dismissal") < result.code.index(
            f'await page.locator("{fill_selector}").fill'
        )

    @pytest.mark.parametrize(
        ("click_selector", "click_role", "dismissal_line", "forbidden_snippet"),
        [
            pytest.param(
                "button:not(.decline)",
                "button",
                "    _scout_optional_dismissal = page.locator(\"button:has-text('Accept')\")",
                'page.locator("button:not(.decline)")',
                id="not-decline",
            ),
            pytest.param(
                ".btns button:nth-of-type(2)",
                "button",
                '    _scout_optional_dismissal = page.locator(".btns button:nth-of-type(2)")',
                'await page.locator(".btns button:nth-of-type(2)").click()',
                id="structural-nth-of-type",
            ),
            pytest.param(
                "//button[normalize-space()='Accept']",
                None,
                "    _scout_optional_dismissal = page.locator(\"button:has-text('Accept')\")",
                "//button[normalize-space()='Accept']",
                id="bare-accept-xpath",
            ),
        ],
    )
    def test_one_step_optional_dismissal_is_not_entry_target(
        self,
        click_selector: str,
        click_role: str | None,
        dismissal_line: str,
        forbidden_snippet: str,
    ) -> None:
        trajectory = [
            _interaction(
                "click",
                selector=click_selector,
                source_url="https://example.com/find",
                role=click_role,
            ),
        ]
        assert is_optional_dismissal_only_trajectory(trajectory) is True

        result = synthesize_code_block(trajectory, strict_selectors=True)

        assert result is not None
        lines = result.code.splitlines()
        assert lines[0] == '    await page.goto("https://example.com/find", wait_until="domcontentloaded")'
        assert dismissal_line in lines
        assert "            await _scout_optional_dismissal.first.click(timeout=1000)" in lines
        assert 'await _scout_entry_target.wait_for(state="visible")' not in result.code
        assert forbidden_snippet not in result.code
        ast.parse("async def _block(page):\n" + result.code)

    def test_terminal_anonymous_structural_click_after_required_is_emitted_as_required(self) -> None:
        terminal_selector = (
            'xpath=/*[name()="html"][1]/*[name()="body"][1]/*[name()="div"][1]/*[name()="div"][2]/*[name()="button"][2]'
        )
        trajectory = [
            _interaction(
                "type_text",
                selector="#locInput",
                source_url="https://example.com/find",
                role="textbox",
                accessible_name="City, county, or ZIP code",
                typed_value="Example City",
            ),
            _interaction(
                "click",
                selector="button.primary",
                source_url="https://example.com/find",
                role="button",
                accessible_name="Search",
            ),
            _interaction(
                "click",
                selector=terminal_selector,
                source_url="https://example.com/results",
                role="button",
            ),
        ]

        result = synthesize_code_block(trajectory, strict_selectors=True)

        assert result is not None
        assert "button:has-text('Accept')" not in result.code
        assert "_scout_optional_dismissal" not in result.code
        terminal_records = [
            record for record in result.diagnostics.emitted_interactions if record.get("trajectory_index") == 2
        ]
        assert len(terminal_records) == 1
        assert not terminal_records[0].get("lane")
        assert terminal_records[0]["method"] == "click"
        assert terminal_records[0]["selector"] == terminal_selector

    def test_terminal_dismissal_click_before_empty_key_press_is_still_reclassified_required(self) -> None:
        # A trailing empty-key press_key is dropped (missing_key) and emits nothing, so it must not steal the
        # terminal index from the anonymous-structural dismissal click before it and defeat the reclassify.
        terminal_selector = (
            'xpath=/*[name()="html"][1]/*[name()="body"][1]/*[name()="div"][1]/*[name()="div"][2]/*[name()="button"][2]'
        )
        trajectory = [
            _interaction(
                "type_text",
                selector="#locInput",
                source_url="https://example.com/find",
                role="textbox",
                accessible_name="City, county, or ZIP code",
                typed_value="Example City",
            ),
            _interaction(
                "click",
                selector=terminal_selector,
                source_url="https://example.com/results",
                role="button",
            ),
            _interaction("press_key", key="", source_url="https://example.com/results"),
        ]

        result = synthesize_code_block(trajectory, strict_selectors=True)

        assert result is not None
        assert "_scout_optional_dismissal" not in result.code
        terminal_records = [
            record for record in result.diagnostics.emitted_interactions if record.get("trajectory_index") == 1
        ]
        assert len(terminal_records) == 1
        assert not terminal_records[0].get("lane")
        assert terminal_records[0]["method"] == "click"
        assert terminal_records[0]["selector"] == terminal_selector

    def test_terminal_structural_click_without_prior_required_stays_in_lane(self) -> None:
        trajectory = [
            _interaction(
                "click",
                selector=".btns button:nth-of-type(2)",
                source_url="https://example.com/find",
                role="button",
            ),
        ]

        result = synthesize_code_block(trajectory, strict_selectors=True)

        assert result is not None
        assert '_scout_optional_dismissal = page.locator(".btns button:nth-of-type(2)")' in result.code
        assert 'await page.locator(".btns button:nth-of-type(2)").click()' not in result.code

    def test_terminal_text_matched_dismissal_after_required_stays_in_lane(self) -> None:
        trajectory = [
            _interaction(
                "type_text",
                selector="#locInput",
                source_url="https://example.com/find",
                role="textbox",
                accessible_name="City, county, or ZIP code",
                typed_value="Example City",
            ),
            _interaction(
                "click",
                selector="button:not(.decline)",
                source_url="https://example.com/find",
                role="button",
            ),
        ]

        result = synthesize_code_block(trajectory, strict_selectors=True)

        assert result is not None
        assert "_scout_optional_dismissal = page.locator(\"button:has-text('Accept')\")" in result.code
        assert 'await page.locator("button:not(.decline)").click()' not in result.code

    def test_optional_dismissal_with_durable_target_is_offerable(self) -> None:
        trajectory = [
            _interaction(
                "click",
                selector="button:not(.decline)",
                source_url="https://example.com/find",
                role="button",
            ),
            _interaction(
                "type_text",
                selector="#locInput",
                source_url="https://example.com/find",
                role="textbox",
                accessible_name="City, county, or ZIP code",
                typed_value="Example City",
            ),
        ]

        assert is_optional_dismissal_only_trajectory(trajectory) is False

    def test_press_enter_uses_keyboard_when_no_selector(self) -> None:
        result = synthesize_code_block([_interaction("press_key", key="Enter")])
        assert result is not None
        assert 'await page.keyboard.press("Enter")' in result.code

    def test_press_key_on_located_element(self) -> None:
        result = synthesize_code_block(
            [
                _interaction(
                    "press_key",
                    selector='role=textbox[name="Search"]',
                    key="Enter",
                    role="textbox",
                    accessible_name="Search",
                )
            ]
        )
        assert result is not None
        assert 'await page.get_by_role("textbox", name="Search").press("Enter")' in result.code

    def test_select_option_emits_value(self) -> None:
        result = synthesize_code_block(
            [
                _interaction(
                    "select_option",
                    selector='role=combobox[name="Size"]',
                    source_url="https://example.com/",
                    value="large",
                    role="combobox",
                    accessible_name="Size",
                )
            ]
        )
        assert result is not None
        assert 'await page.get_by_role("combobox", name="Size").select_option("large")' in result.code

    def test_select_option_without_value_is_dropped_with_note(self) -> None:
        result = synthesize_code_block(
            [
                _interaction(
                    "select_option",
                    selector='role=combobox[name="Size"]',
                    source_url="https://example.com/",
                    role="combobox",
                    accessible_name="Size",
                )
            ]
        )
        assert result is None or "select_option" not in result.code
        if result is not None:
            assert any("select_option" in note for note in result.notes)


class TestParamKeySafety:
    @staticmethod
    def _emitted_wrapper(code: str, param_keys: list[str]) -> str:
        # Mirror block.py generate_async_user_function: the param keys become the wrapper signature.
        signature = ", ".join(f"{key}=None" for key in param_keys)
        body = "\n".join(f"    {line}" for line in code.splitlines())
        return f"async def wrapper({signature}):\n{body or '    pass'}"

    def test_keyword_accessible_name_yields_bindable_identifier(self) -> None:
        result = synthesize_code_block(
            [
                _interaction(
                    "type_text",
                    selector='role=textbox[name="Class"]',
                    source_url="https://example.com/",
                    typed_length=4,
                    role="textbox",
                    accessible_name="Class",
                )
            ]
        )
        assert result is not None
        keys = [p["key"] for p in result.parameters]
        assert keys == ["class_field"]
        assert all(key.isidentifier() and not keyword.iskeyword(key) for key in keys)
        ast.parse(self._emitted_wrapper(result.code, keys))

    def test_reserved_safe_var_name_is_suffixed(self) -> None:
        result = synthesize_code_block(
            [
                _interaction(
                    "type_text",
                    selector='role=textbox[name="Page"]',
                    source_url="https://example.com/",
                    typed_length=4,
                    role="textbox",
                    accessible_name="Page",
                )
            ]
        )
        assert result is not None
        assert result.parameters == [{"key": "page_field"}]
        assert "fill(str(page))" not in result.code
        assert "fill(str(page_field))" in result.code

    def test_leading_digit_name_is_valid_identifier(self) -> None:
        result = synthesize_code_block(
            [
                _interaction(
                    "type_text",
                    selector='role=textbox[name="2nd line"]',
                    source_url="https://example.com/",
                    typed_length=4,
                    role="textbox",
                    accessible_name="2nd line",
                )
            ]
        )
        assert result is not None
        keys = [p["key"] for p in result.parameters]
        assert keys and all(key.isidentifier() for key in keys)
        ast.parse(self._emitted_wrapper(result.code, keys))


class TestTrajectoryFidelity:
    def test_two_same_selector_clicks_both_emitted(self) -> None:
        # Regression: scout_trajectory is append-only/non-deduped, so a repeated
        # click on the same selector must produce two clicks (the deduped list would drop one).
        trajectory = [
            _interaction(
                "click",
                selector='role=button[name="Add to cart"]',
                source_url="https://example.com/p",
                role="button",
                accessible_name="Add to cart",
            ),
            _interaction(
                "click",
                selector='role=button[name="Add to cart"]',
                source_url="https://example.com/p",
                role="button",
                accessible_name="Add to cart",
            ),
        ]
        result = synthesize_code_block(trajectory)
        assert result is not None
        assert result.code.count('await page.get_by_role("button", name="Add to cart").click()') == 2

    def test_same_name_disambiguation_keeps_distinct_param_keys(self) -> None:
        trajectory = [
            _interaction(
                "type_text",
                selector='role=textbox[name="Name"]',
                source_url="https://example.com/",
                typed_length=3,
                role="textbox",
                accessible_name="Name",
            ),
            _interaction(
                "type_text",
                selector='role=textbox[name="Name"]',
                source_url="https://example.com/",
                typed_length=3,
                role="textbox",
                accessible_name="Name",
            ),
        ]
        result = synthesize_code_block(trajectory)
        assert result is not None
        assert result.parameters == [{"key": "name"}, {"key": "name_2"}]
        assert "fill(str(name))" in result.code
        assert "fill(str(name_2))" in result.code

    def test_param_keys_are_globally_unique_against_external_slug_collision(self) -> None:
        # An externally-derived slug "name 2" produces base "name_2", which must not collide with the
        # auto-suffix of two "name" fields. All keys are tracked in one global used-set.
        trajectory = [
            _interaction(
                "type_text",
                selector="#a",
                source_url="https://example.com/",
                typed_length=3,
                role="textbox",
                accessible_name="Name",
            ),
            _interaction(
                "type_text",
                selector="#b",
                source_url="https://example.com/",
                typed_length=3,
                role="textbox",
                accessible_name="name 2",
            ),
            _interaction(
                "type_text",
                selector="#c",
                source_url="https://example.com/",
                typed_length=3,
                role="textbox",
                accessible_name="Name",
            ),
        ]
        result = synthesize_code_block(trajectory)
        assert result is not None
        keys = [p["key"] for p in result.parameters]
        assert keys == ["name", "name_2", "name_3"]
        assert len(set(keys)) == len(keys)
        for key in keys:
            assert f"fill(str({key}))" in result.code

    def test_step_cap_truncates_at_configured_limit(self) -> None:
        trajectory = [
            _interaction("click", selector=f'role=button[name="b{i}"]', source_url="https://example.com/")
            for i in range(_MAX_STEPS + 5)
        ]
        result = synthesize_code_block(trajectory)
        assert result is not None
        assert result.code.count(".click()") == _MAX_STEPS
        assert result.diagnostics.truncated is True
        assert any("truncated" in note for note in result.notes)

    def test_strict_synthesis_emits_byte_equal_selector_provenance(self) -> None:
        result = synthesize_code_block(
            [
                _interaction(
                    "type_text",
                    selector="#searchInput",
                    source_url="https://example.com/find-care",
                    typed_length=13,
                    role="textbox",
                    accessible_name="Entity Name",
                )
            ],
            strict_selectors=True,
        )

        assert result is not None
        assert 'await page.locator("#searchInput").fill(str(entity_name))' in result.code
        assert result.diagnostics.dropped_interactions == []
        assert result.diagnostics.locator_provenance == [
            {
                "trajectory_index": 0,
                "selector": "#searchInput",
                "emitted_literal": "#searchInput",
                "source": "selector",
            }
        ]

    def test_strict_synthesis_reports_unsupported_interaction(self) -> None:
        result = synthesize_code_block(
            [
                _interaction("click", selector="#open", source_url="https://example.com/"),
                _interaction("hover", selector="#menu", source_url="https://example.com/"),
            ],
            strict_selectors=True,
        )

        assert result is not None
        assert result.diagnostics.dropped_interactions == [
            {"trajectory_index": 1, "tool_name": "hover", "reason_code": "unsupported_tool"}
        ]

    def test_synthesis_scrubs_credentials_from_emitted_url_literals(self) -> None:
        result = synthesize_code_block(
            [
                _interaction(
                    "click",
                    selector="#go",
                    source_url="https://user:password@example.com/search?token=secret-token&q=record#access_token=fragment-token&section=results",
                )
            ]
        )
        metadata = build_synthesized_artifact_metadata(
            [
                _interaction(
                    "click",
                    selector="#go",
                    source_url="https://user:password@example.com/search?token=secret-token&q=record#access_token=fragment-token&section=results",
                )
            ]
        )

        assert result is not None
        assert "user:password" not in result.code
        assert "secret-token" not in result.code
        assert "fragment-token" not in result.code
        assert "q=record" in result.code
        assert "section=results" in result.code
        page_dependency = metadata["page_dependencies"][0]
        assert page_dependency["url_hint"] == (
            "https://example.com/search?token=__redacted__&q=record#access_token=__redacted__&section=results"
        )

    def test_synthesis_scrubs_bare_sensitive_url_fragments(self) -> None:
        result = synthesize_code_block(
            [
                _interaction(
                    "click",
                    selector="#go",
                    source_url="https://example.com/search?q=record#secret-token-fragment",
                )
            ]
        )
        metadata = build_synthesized_artifact_metadata(
            [
                _interaction(
                    "click",
                    selector="#go",
                    source_url="https://example.com/search?q=record#secret-token-fragment",
                )
            ]
        )

        assert result is not None
        assert "secret-token-fragment" not in result.code
        assert (
            'await page.goto("https://example.com/search?q=record#__redacted__", wait_until="domcontentloaded")'
            in result.code
        )
        assert metadata["page_dependencies"][0]["url_hint"] == "https://example.com/search?q=record#__redacted__"


class TestDeterminismAndEmpty:
    def test_empty_trajectory_returns_none(self) -> None:
        assert synthesize_code_block([]) is None

    def test_byte_identical_per_trajectory(self) -> None:
        trajectory = [
            _interaction(
                "type_text",
                selector='role=textbox[name="Search"]',
                source_url="https://example.com/",
                typed_length=4,
                role="textbox",
                accessible_name="Search",
            ),
            _interaction("press_key", key="Enter"),
            _interaction(
                "click",
                selector='role=button[name="Go"]',
                role="button",
                accessible_name="Go",
            ),
        ]
        first = synthesize_code_block(trajectory)
        second = synthesize_code_block(trajectory)
        assert first is not None and second is not None
        assert first.code == second.code
        assert first.parameters == second.parameters
        assert first.notes == second.notes


class TestStepEmission:
    def test_synthesize_emits_goal_ready_steps(self) -> None:
        trajectory = [
            _interaction("click", selector="#go", source_url="https://example.com"),
            _interaction("type_text", selector="#q", typed_length=5),
        ]
        block = synthesize_code_block(trajectory)
        assert block is not None
        assert [s["action_type"] for s in block.steps] == ["goto_url", "click", "input_text"]
        assert all(s["description"] for s in block.steps)

    def test_step_line_spans_cover_every_emitted_line(self) -> None:
        trajectory = [
            _interaction("click", selector="#go", source_url="https://example.com/start"),
            _interaction(
                "type_text",
                selector="#q",
                typed_length=5,
                role="textbox",
                accessible_name="Search",
            ),
            _interaction("press_key", key="Enter"),
        ]
        block = synthesize_code_block(trajectory)
        assert block is not None
        code_lines = block.code.splitlines()
        goto_step, click_step, fill_step, key_step = block.steps
        assert (goto_step["line_start"], goto_step["line_end"]) == (1, 6)
        assert code_lines[goto_step["line_start"] - 1].lstrip().startswith("_scout_entry_target = ")
        assert (click_step["line_start"], click_step["line_end"]) == (7, 8)
        assert ".click()" in code_lines[click_step["line_start"] - 1]
        assert (fill_step["line_start"], fill_step["line_end"]) == (9, 9)
        assert ".fill(" in code_lines[fill_step["line_start"] - 1]
        assert key_step["action_type"] == "keypress"
        assert (key_step["line_start"], key_step["line_end"]) == (10, len(code_lines))
        assert "press" in code_lines[key_step["line_start"] - 1]
        # Spans are contiguous and cover the whole block.
        assert block.steps[0]["line_start"] == 1
        assert block.steps[-1]["line_end"] == len(code_lines)
        for previous, current in zip(block.steps, block.steps[1:]):
            assert current["line_start"] == previous["line_end"] + 1

    def test_select_option_and_press_key_action_types(self) -> None:
        trajectory = [
            _interaction(
                "select_option",
                selector='role=combobox[name="Size"]',
                source_url="https://example.com/",
                value="large",
                role="combobox",
                accessible_name="Size",
            ),
            _interaction("press_key", key="Enter"),
        ]
        block = synthesize_code_block(trajectory)
        assert block is not None
        assert [s["action_type"] for s in block.steps] == ["goto_url", "select_option", "keypress"]

    def test_skipped_interactions_emit_no_step(self) -> None:
        trajectory = [
            _interaction("click", selector="#go", source_url="https://example.com/"),
            _interaction("select_option", selector="#size"),
            _interaction("click"),
            _interaction("press_key", key=""),
        ]
        block = synthesize_code_block(trajectory)
        assert block is not None
        assert [s["action_type"] for s in block.steps] == ["goto_url", "click"]

    def test_hover_emits_step_in_non_strict_mode(self) -> None:
        trajectory = [
            _interaction("click", selector="#go", source_url="https://example.com/"),
            _interaction("hover", selector="#menu"),
        ]
        block = synthesize_code_block(trajectory)
        assert block is not None
        assert [s["action_type"] for s in block.steps] == ["goto_url", "click", "hover"]
        assert 'await page.locator("#menu").hover()' in block.code

    def test_hover_stays_unsupported_in_strict_mode(self) -> None:
        block = synthesize_code_block(
            [
                _interaction("click", selector="#open", source_url="https://example.com/"),
                _interaction("hover", selector="#menu"),
            ],
            strict_selectors=True,
        )
        assert block is not None
        assert {"trajectory_index": 1, "tool_name": "hover", "reason_code": "unsupported_tool"} in (
            block.diagnostics.dropped_interactions
        )

    def test_wait_emits_timeout_step(self) -> None:
        trajectory = [
            _interaction("click", selector="#go", source_url="https://example.com/"),
            {"tool_name": "wait", "duration_ms": 6000},
        ]
        block = synthesize_code_block(trajectory)
        assert block is not None
        assert [s["action_type"] for s in block.steps] == ["goto_url", "click", "wait"]
        assert "await page.wait_for_timeout(6000)" in block.code

    def test_no_entry_url_means_no_goto_step(self) -> None:
        block = synthesize_code_block([_interaction("press_key", key="Enter")])
        assert block is not None
        assert [s["action_type"] for s in block.steps] == ["keypress"]

    def test_step_descriptions_prefer_accessible_name_over_selector(self) -> None:
        block = synthesize_code_block(
            [
                _interaction(
                    "click",
                    selector="div.list > button:nth-of-type(3)",
                    source_url="https://example.com/",
                    role="button",
                    accessible_name="Add to cart",
                )
            ]
        )
        assert block is not None
        click_step = next(s for s in block.steps if s["action_type"] == "click")
        assert "Add to cart" in click_step["description"]
        assert "nth-of-type" not in click_step["description"]

    def test_entry_url_step_description_carries_url(self) -> None:
        block = synthesize_code_block([_interaction("click", selector="#go", source_url="https://example.com/start")])
        assert block is not None
        goto_step = block.steps[0]
        assert goto_step["action_type"] == "goto_url"
        assert "https://example.com/start" in goto_step["description"]

    def test_steps_are_byte_identical_per_trajectory(self) -> None:
        trajectory = [
            _interaction(
                "type_text",
                selector='role=textbox[name="Search"]',
                source_url="https://example.com/",
                typed_length=4,
                role="textbox",
                accessible_name="Search",
            ),
            _interaction("press_key", key="Enter"),
        ]
        first = synthesize_code_block(trajectory)
        second = synthesize_code_block(trajectory)
        assert first is not None and second is not None
        assert first.steps == second.steps

    def test_truncated_trajectory_caps_steps_with_code(self) -> None:
        trajectory = [
            _interaction("click", selector=f'role=button[name="b{i}"]', source_url="https://example.com/")
            for i in range(_MAX_STEPS + 5)
        ]
        block = synthesize_code_block(trajectory)
        assert block is not None
        click_steps = [s for s in block.steps if s["action_type"] == "click"]
        assert len(click_steps) == _MAX_STEPS
        assert block.steps[-1]["line_end"] == len(block.code.splitlines())

    def test_steps_validate_against_code_block_step_schema(self) -> None:
        block = synthesize_code_block(_SCOUT_TRAJECTORY)
        assert block is not None
        validated = [CodeBlockStep(**step) for step in block.steps]
        assert all(step.line_start is not None and step.line_end is not None for step in validated)


class TestLineBoundaryEscaping:
    # str.splitlines() and several parsers treat each of these as a line boundary. An attacker-controlled
    # page can plant one in an accessible name or option value; left unescaped it splits the emitted
    # one-line literal across lines and corrupts the block (availability, not RCE — the leading quote
    # precedes the payload and every attacker quote is escaped).
    _BOUNDARY_CODEPOINTS = ("\x0b", "\x0c", "\x85", " ", " ")

    @staticmethod
    def _parses(code: str) -> ast.Module:
        wrapper = "async def __wrapper__(payload=None):\n" + "\n".join(f"    {line}" for line in code.splitlines())
        return ast.parse(wrapper)

    def test_accessible_name_boundary_codepoints_keep_block_parseable(self) -> None:
        for codepoint in self._BOUNDARY_CODEPOINTS:
            name = f"Search{codepoint}payload"
            result = synthesize_code_block(
                [
                    _interaction(
                        "click",
                        selector=f'role=button[name="{name}"]',
                        source_url="https://example.com/",
                        role="button",
                        accessible_name=name,
                    )
                ]
            )
            assert result is not None, f"codepoint U+{ord(codepoint):04X} produced no block"
            # The block parses with no SyntaxError despite the raw line boundary in the name.
            self._parses(result.code)
            # The raw codepoint never reaches the emitted source; it survives only as a backslash escape,
            # so the payload stays inert inside the single-line literal.
            assert codepoint not in result.code, f"raw U+{ord(codepoint):04X} leaked into emitted code"
            assert "payload" in result.code

    def test_select_option_value_boundary_codepoint_keeps_block_parseable(self) -> None:
        value = "small\x0bvalue"
        result = synthesize_code_block(
            [
                _interaction(
                    "select_option",
                    selector='role=combobox[name="Size"]',
                    source_url="https://example.com/",
                    value=value,
                    role="combobox",
                    accessible_name="Size",
                )
            ]
        )
        assert result is not None
        self._parses(result.code)
        assert "\x0b" not in result.code
        assert "select_option" in result.code

    def test_c0_control_codepoint_in_name_is_escaped(self) -> None:
        # Belt-and-suspenders: the defensive C0/C1 control pass also escapes non-line-boundary controls.
        name = "Search\x07bell"
        result = synthesize_code_block(
            [
                _interaction(
                    "click",
                    selector=f'role=button[name="{name}"]',
                    source_url="https://example.com/",
                    role="button",
                    accessible_name=name,
                )
            ]
        )
        assert result is not None
        self._parses(result.code)
        assert "\x07" not in result.code


class TestPreflightSurfacesSyntaxError:
    def test_synthesized_block_round_trips_through_preflight(self) -> None:
        # A well-formed synthesized block (boundary codepoint escaped) yields no syntax diagnostic.
        result = synthesize_code_block(
            [
                _interaction(
                    "click",
                    selector='role=button[name="Go\x0bx"]',
                    source_url="https://example.com/",
                    role="button",
                    accessible_name="Go\x0bx",
                )
            ]
        )
        assert result is not None
        diagnostics = preflight_code_block(result.code, parameter_keys=())
        assert not any(d.code == "SYNTAX_ERROR" for d in diagnostics)

    def test_unparseable_block_surfaces_syntax_diagnostic(self) -> None:
        # A malformed block must be caught at authoring time, not swallowed into a silent run-time failure.
        diagnostics = preflight_code_block('await page.goto("unterminated)\n', parameter_keys=())
        assert any(d.code == "SYNTAX_ERROR" for d in diagnostics)

    @pytest.mark.parametrize(
        ("code", "reason"),
        [
            ("await page.request.post('https://example.com/collect')", "AUTHOR_PAGE_REQUEST"),
            ("state = await page.context.storage_state()", "AUTHOR_PAGE_CONTEXT"),
            ("text = await page.evaluate('() => document.body.innerText')", "AUTHOR_PAGE_EVALUATE"),
            ("handle = await page.evaluate_handle('() => document.body')", "AUTHOR_PAGE_EVALUATE"),
        ],
    )
    def test_denied_page_api_attributes_surface_preflight_reason_codes(self, code: str, reason: str) -> None:
        diagnostics = preflight_code_block(code, parameter_keys=())

        assert [diagnostic.code for diagnostic in diagnostics if diagnostic.code.startswith("AUTHOR_PAGE_")] == [reason]
        assert any("not allowed in persisted workflow code blocks" in diagnostic.message for diagnostic in diagnostics)

    def test_denied_page_api_preflight_reason_codes_match_author_time_security_source(self) -> None:
        code = """
        await page.request.post("https://example.com/collect")
        state = await page.context.storage_state()
        text = await page.evaluate("() => document.body.innerText")
        handle = await page.evaluate_handle("() => document.body")
        """

        normalized_code = textwrap.dedent(code).strip()
        diagnostics = preflight_code_block(code, parameter_keys=())
        security_errors = author_time_code_security_errors(label="search_registry", code=normalized_code)

        preflight_reasons = {
            diagnostic.code for diagnostic in diagnostics if diagnostic.code.startswith("AUTHOR_PAGE_")
        }
        security_reasons = {error.reason_code for error in security_errors}
        assert (
            preflight_reasons
            == security_reasons
            == {
                "AUTHOR_PAGE_REQUEST",
                "AUTHOR_PAGE_CONTEXT",
                "AUTHOR_PAGE_EVALUATE",
            }
        )

    def test_broad_body_text_wait_for_function_surfaces_selection_diagnostic(self) -> None:
        code = (
            "await page.wait_for_function("
            "\"() => document.body.innerText.includes('Details') || "
            "document.body.innerText.includes('Nothing was found')\", timeout=5000)\n"
        )

        diagnostics = preflight_code_block(code, parameter_keys=())

        assert sum(1 for d in diagnostics if d.code == "BROAD_DOCUMENT_BODY_TEXT_WAIT") == 1
        assert any("localized container" in d.message for d in diagnostics)

    def test_broad_body_text_wait_for_function_keyword_expression_surfaces_selection_diagnostic(self) -> None:
        code = (
            "await page.wait_for_function("
            "expression=\"() => document.body.innerText.includes('Details')\", timeout=5000)\n"
        )

        diagnostics = preflight_code_block(code, parameter_keys=())

        assert sum(1 for d in diagnostics if d.code == "BROAD_DOCUMENT_BODY_TEXT_WAIT") == 1

    def test_localized_detail_locator_wait_does_not_surface_body_text_diagnostic(self) -> None:
        code = (
            'await page.locator("main").get_by_text("Details").wait_for(timeout=5000)\n'
            'return {"entity_name": await page.locator("h1").inner_text(timeout=5000)}\n'
        )

        diagnostics = preflight_code_block(code, parameter_keys=())

        assert not any(d.code == "BROAD_DOCUMENT_BODY_TEXT_WAIT" for d in diagnostics)

    def test_global_get_by_text_wait_for_surfaces_ambiguous_locator_diagnostic(self) -> None:
        diagnostics = preflight_code_block(
            'await page.get_by_text("NATIONAL PROVIDER IDENTIFIER").wait_for(timeout=5000)\n',
            parameter_keys=(),
        )

        assert sum(1 for d in diagnostics if d.code == "GLOBAL_GET_BY_TEXT_WAIT_FOR") == 1

    def test_global_get_by_text_wait_for_alias_surfaces_ambiguous_locator_diagnostic(self) -> None:
        diagnostics = preflight_code_block(
            'target = page.get_by_text("NATIONAL PROVIDER IDENTIFIER")\nawait target.wait_for(timeout=5000)\n',
            parameter_keys=(),
        )

        assert sum(1 for d in diagnostics if d.code == "GLOBAL_GET_BY_TEXT_WAIT_FOR") == 1

    @pytest.mark.parametrize(
        "code",
        [
            "if ready:\n"
            '    target = page.get_by_text("NATIONAL PROVIDER IDENTIFIER")\n'
            "    await target.wait_for(timeout=5000)\n",
            "for _ in [1]:\n"
            '    target = page.get_by_text("NATIONAL PROVIDER IDENTIFIER")\n'
            "    await target.wait_for(timeout=5000)\n",
            "try:\n"
            '    target = page.get_by_text("NATIONAL PROVIDER IDENTIFIER")\n'
            "    await target.wait_for(timeout=5000)\n"
            "except Exception:\n"
            "    pass\n",
        ],
    )
    def test_nested_get_by_text_alias_wait_surfaces_ambiguous_locator_diagnostic(self, code: str) -> None:
        diagnostics = preflight_code_block(code, parameter_keys=("ready",))

        assert sum(1 for d in diagnostics if d.code == "GLOBAL_GET_BY_TEXT_WAIT_FOR") == 1

    def test_rebound_get_by_text_alias_does_not_surface_stale_ambiguous_locator_diagnostic(self) -> None:
        diagnostics = preflight_code_block(
            'target = page.get_by_text("NATIONAL PROVIDER IDENTIFIER")\n'
            'target = page.locator("#safe")\n'
            "await target.wait_for(timeout=5000)\n",
            parameter_keys=(),
        )

        assert not any(d.code == "GLOBAL_GET_BY_TEXT_WAIT_FOR" for d in diagnostics)

    def test_get_by_text_alias_wait_before_rebind_surfaces_ambiguous_locator_diagnostic(self) -> None:
        diagnostics = preflight_code_block(
            'target = page.get_by_text("NATIONAL PROVIDER IDENTIFIER")\n'
            "await target.wait_for(timeout=5000)\n"
            'target = page.locator("#safe")\n',
            parameter_keys=(),
        )

        assert sum(1 for d in diagnostics if d.code == "GLOBAL_GET_BY_TEXT_WAIT_FOR") == 1

    def test_nested_earlier_get_by_text_rebind_does_not_clear_later_global_alias(self) -> None:
        diagnostics = preflight_code_block(
            "async def helper():\n"
            '    target = page.locator("#safe")\n'
            'target = page.get_by_text("NATIONAL PROVIDER IDENTIFIER")\n'
            "await target.wait_for(timeout=5000)\n",
            parameter_keys=(),
        )

        assert sum(1 for d in diagnostics if d.code == "GLOBAL_GET_BY_TEXT_WAIT_FOR") == 1

    def test_nested_later_get_by_text_rebind_does_not_clear_outer_global_alias(self) -> None:
        diagnostics = preflight_code_block(
            'target = page.get_by_text("NATIONAL PROVIDER IDENTIFIER")\n'
            "async def helper():\n"
            '    target = page.locator("#safe")\n'
            "await target.wait_for(timeout=5000)\n",
            parameter_keys=(),
        )

        assert sum(1 for d in diagnostics if d.code == "GLOBAL_GET_BY_TEXT_WAIT_FOR") == 1

    @pytest.mark.parametrize(
        "code",
        [
            'await page.locator("table").wait_for(state="visible", timeout=15000)\n',
            'await page.locator("table").first.wait_for(state="visible", timeout=15000)\n',
            'table = page.locator("table")\nawait table.first.wait_for(state="visible", timeout=15000)\n',
        ],
    )
    def test_broad_global_table_wait_for_surfaces_selection_diagnostic(self, code: str) -> None:
        diagnostics = preflight_code_block(code, parameter_keys=())

        assert sum(1 for d in diagnostics if d.code == "BROAD_GLOBAL_TABLE_WAIT_FOR") == 1

    @pytest.mark.parametrize(
        "code",
        [
            'if ready:\n    target = page.locator("table")\n    await target.wait_for(timeout=5000)\n',
            'for _ in [1]:\n    target = page.locator("table")\n    await target.wait_for(timeout=5000)\n',
            "try:\n"
            '    target = page.locator("table")\n'
            "    await target.wait_for(timeout=5000)\n"
            "except Exception:\n"
            "    pass\n",
        ],
    )
    def test_nested_table_alias_wait_surfaces_broad_table_diagnostic(self, code: str) -> None:
        diagnostics = preflight_code_block(code, parameter_keys=("ready",))

        assert sum(1 for d in diagnostics if d.code == "BROAD_GLOBAL_TABLE_WAIT_FOR") == 1

    @pytest.mark.parametrize(
        "code",
        [
            'await page.locator("table tbody tr").first.wait_for(state="visible", timeout=15000)\n',
            'await page.locator("#coastalCard").locator("table").first.wait_for(state="visible", timeout=15000)\n',
            'await page.locator("table").get_by_role("row").first.wait_for(state="visible", timeout=15000)\n',
            'await page.locator("table").filter(has_text="Credentialed").wait_for(timeout=15000)\n',
            'table = page.locator("table").filter(has_text="Credentialed")\nawait table.wait_for(timeout=15000)\n',
        ],
    )
    def test_scoped_or_narrowed_table_wait_for_is_allowed(self, code: str) -> None:
        diagnostics = preflight_code_block(code, parameter_keys=())

        assert not any(d.code == "BROAD_GLOBAL_TABLE_WAIT_FOR" for d in diagnostics)

    def test_rebound_table_alias_does_not_surface_stale_broad_table_diagnostic(self) -> None:
        diagnostics = preflight_code_block(
            'target = page.locator("table")\ntarget = page.locator("#safe")\nawait target.wait_for(timeout=5000)\n',
            parameter_keys=(),
        )

        assert not any(d.code == "BROAD_GLOBAL_TABLE_WAIT_FOR" for d in diagnostics)

    def test_table_alias_wait_before_rebind_surfaces_broad_table_diagnostic(self) -> None:
        diagnostics = preflight_code_block(
            'target = page.locator("table")\nawait target.wait_for(timeout=5000)\ntarget = page.locator("#safe")\n',
            parameter_keys=(),
        )

        assert sum(1 for d in diagnostics if d.code == "BROAD_GLOBAL_TABLE_WAIT_FOR") == 1

    def test_nested_earlier_table_rebind_does_not_clear_later_global_alias(self) -> None:
        diagnostics = preflight_code_block(
            "async def helper():\n"
            '    target = page.locator("#safe")\n'
            'target = page.locator("table")\n'
            "await target.wait_for(timeout=5000)\n",
            parameter_keys=(),
        )

        assert sum(1 for d in diagnostics if d.code == "BROAD_GLOBAL_TABLE_WAIT_FOR") == 1

    def test_nested_later_table_rebind_does_not_clear_outer_global_alias(self) -> None:
        diagnostics = preflight_code_block(
            'target = page.locator("table")\n'
            "async def helper():\n"
            '    target = page.locator("#safe")\n'
            "await target.wait_for(timeout=5000)\n",
            parameter_keys=(),
        )

        assert sum(1 for d in diagnostics if d.code == "BROAD_GLOBAL_TABLE_WAIT_FOR") == 1

    @pytest.mark.parametrize(
        "code",
        [
            'await page.locator("main").get_by_text("NATIONAL PROVIDER IDENTIFIER").wait_for(timeout=5000)\n',
            'await page.get_by_text("NATIONAL PROVIDER IDENTIFIER").first.wait_for(timeout=5000)\n',
            'await page.get_by_text("NATIONAL PROVIDER IDENTIFIER").nth(0).wait_for(timeout=5000)\n',
            'await page.get_by_text("NATIONAL PROVIDER IDENTIFIER").filter(visible=True).wait_for(timeout=5000)\n',
        ],
    )
    def test_scoped_or_narrowed_get_by_text_wait_for_is_allowed(self, code: str) -> None:
        diagnostics = preflight_code_block(code, parameter_keys=())

        assert not any(d.code == "GLOBAL_GET_BY_TEXT_WAIT_FOR" for d in diagnostics)

    def test_non_page_wait_for_function_does_not_surface_body_text_diagnostic(self) -> None:
        code = """
        diagnostics = []
        await custom_waiter.wait_for_function("() => document.body.innerText.includes('Ready')")
        return {"diagnostics": diagnostics}
        """

        diagnostics = preflight_code_block(code, parameter_keys=("custom_waiter",))

        assert not any(d.code == "BROAD_DOCUMENT_BODY_TEXT_WAIT" for d in diagnostics)

    def test_persist_safety_rejects_broad_body_text_wait_for_function(self) -> None:
        workflow_yaml = (
            "title: Record lookup\n"
            "workflow_definition:\n"
            "  blocks:\n"
            "    - block_type: code\n"
            "      label: check_record_status_status\n"
            "      code: |\n"
            "        await page.wait_for_function(\"() => document.body.innerText.includes('Details')\", "
            "timeout=5000)\n"
        )

        errors = _code_block_safety_errors(workflow_yaml, None)

        assert any("failed the generated-code preflight check" in str(error) for error in errors)
        assert any("localized container" in str(error) for error in errors)

    def test_broad_container_record_scan_surfaces_row_extraction_diagnostic(self) -> None:
        code = """
        raw_cards = []
        for selector in ["[class*='result']", "article", "section", ".card", "li"]:
            locs = page.locator(selector)
            for i in range(await locs.count()):
                txt = await locs.nth(i).inner_text()
                if "status" in txt.lower():
                    raw_cards.append(txt)
        items = []
        for txt in raw_cards:
            items.append({
                "item_name": txt.split("\\n")[0],
                "address": txt[:200],
                "status": "Inactive" if "inactive" in txt.lower() else "Active",
            })
        return {"items": items, "overall_status": "Active"}
        """

        diagnostics = preflight_code_block(code, parameter_keys=())

        assert any(d.code == "BROAD_TABLE_RECORD_SCAN" for d in diagnostics)

    def test_embedded_tr_substring_does_not_suppress_broad_scan_diagnostic(self) -> None:
        code = """
        cards = page.locator("section")
        items = []
        for i in range(await cards.count()):
            text = await cards.nth(i).inner_text()
            items.append({
                "record_name": text.split("\\n")[0],
                "street_label": "Street",
                "status": "Active",
            })
        return {"items": items}
        """

        diagnostics = preflight_code_block(code, parameter_keys=())

        assert any(d.code == "BROAD_TABLE_RECORD_SCAN" for d in diagnostics)

    def test_non_selector_section_literal_does_not_surface_broad_scan_diagnostic(self) -> None:
        code = """
        layout_type = "section"
        items = [{"name": "Example"}]
        return {"items": items, "layout_type": layout_type}
        """

        diagnostics = preflight_code_block(code, parameter_keys=())

        assert not any(d.code == "BROAD_TABLE_RECORD_SCAN" for d in diagnostics)

    def test_lone_list_item_selector_does_not_surface_broad_scan_diagnostic(self) -> None:
        code = """
        await page.locator("li").filter(has_text="Status").click()
        return {"items": [], "overall_status": "Active"}
        """

        diagnostics = preflight_code_block(code, parameter_keys=())

        assert not any(d.code == "BROAD_TABLE_RECORD_SCAN" for d in diagnostics)

    def test_status_text_without_record_return_shape_does_not_surface_broad_scan_diagnostic(self) -> None:
        code = """
        sections = page.locator("section")
        for i in range(await sections.count()):
            text = await sections.nth(i).inner_text()
            if "status" in text.lower():
                print("status panel found")
        return {"ok": True}
        """

        diagnostics = preflight_code_block(code, parameter_keys=())

        assert not any(d.code == "BROAD_TABLE_RECORD_SCAN" for d in diagnostics)

    def test_status_only_error_shape_does_not_surface_broad_scan_diagnostic(self) -> None:
        code = """
        section = page.locator("section").first
        if await section.count() == 0:
            return {"status": "missing"}
        await section.click()
        return {"status": "clicked"}
        """

        diagnostics = preflight_code_block(code, parameter_keys=())

        assert not any(d.code == "BROAD_TABLE_RECORD_SCAN" for d in diagnostics)

    def test_table_row_record_extraction_does_not_surface_broad_scan_diagnostic(self) -> None:
        code = """
        rows = page.locator("table tbody tr")
        items = []
        for i in range(await rows.count()):
            row = rows.nth(i)
            cells = row.locator("td")
            if await cells.count() < 3:
                continue
            item_name = " ".join((await cells.nth(0).inner_text()).split())
            address = " ".join((await cells.nth(1).inner_text()).split())
            status = " ".join((await cells.nth(2).inner_text()).split())
            items.append({
                "item_name": item_name,
                "address": address,
                "status": status,
            })
        return {"items": items, "overall_status": "Active"}
        """

        diagnostics = preflight_code_block(code, parameter_keys=())

        assert not any(d.code == "BROAD_TABLE_RECORD_SCAN" for d in diagnostics)


class TestRenderSynthesizedOfferText:
    def test_renders_label_code_and_params(self) -> None:
        synthesized = synthesize_code_block(
            [
                _interaction(
                    "type_text",
                    selector="#searcher_s",
                    source_url="https://example.com/",
                    typed_length=5,
                    role="textbox",
                    accessible_name="Search",
                )
            ]
        )
        assert synthesized is not None
        text = render_synthesized_offer_text(synthesized)
        assert text.startswith("SYNTHESIZED CODE BLOCK (offered once).")
        assert _SYNTHESIZED_BLOCK_LABEL in text
        assert "```python" in text
        assert 'await page.locator("#searcher_s").fill(str(search))' in text
        assert "Workflow parameters referenced (bind these): search." in text

    def test_omits_param_line_when_no_parameters(self) -> None:
        synthesized = synthesize_code_block(
            [
                _interaction(
                    "click",
                    selector='role=button[name="Go"]',
                    source_url="https://example.com/",
                    role="button",
                    accessible_name="Go",
                )
            ]
        )
        assert synthesized is not None
        text = render_synthesized_offer_text(synthesized)
        assert "Workflow parameters referenced" not in text

    def test_includes_synthesis_notes_when_present(self) -> None:
        synthesized = synthesize_code_block(
            [
                _interaction(
                    "click",
                    selector="li:nth-of-type(3) > a",
                    source_url="https://example.com/",
                )
            ]
        )
        assert synthesized is not None
        assert synthesized.notes
        text = render_synthesized_offer_text(synthesized)
        assert "Synthesis notes: " in text


class TestOfferTextGoalAndSteps:
    def test_offer_text_carries_steps_json_and_goal(self) -> None:
        synthesized = synthesize_code_block(_SCOUT_TRAJECTORY)
        assert synthesized is not None
        text = render_synthesized_offer_text(
            synthesized, _SCOUT_TRAJECTORY, goal="Search the catalog and add the item to the cart"
        )
        assert "`steps`" in text
        assert "`prompt`" in text
        assert "Search the catalog and add the item to the cart" in text
        assert '"action_type": "goto_url"' in text
        assert '"action_type": "input_text"' in text

    def test_offer_text_omits_goal_mention_without_goal(self) -> None:
        synthesized = synthesize_code_block(_SCOUT_TRAJECTORY)
        assert synthesized is not None
        text = render_synthesized_offer_text(synthesized, _SCOUT_TRAJECTORY)
        assert "`steps`" in text
        assert "`prompt`" not in text

    def test_offer_text_goal_quotes_and_newlines_stay_in_quoted_span(self) -> None:
        synthesized = synthesize_code_block(_SCOUT_TRAJECTORY)
        assert synthesized is not None
        text = render_synthesized_offer_text(synthesized, _SCOUT_TRAJECTORY, goal='find the "best" deal\nand report it')
        assert "`prompt` field to \"find the 'best' deal and report it\"" in text

    def test_offer_text_goal_code_fences_are_neutralized(self) -> None:
        synthesized = synthesize_code_block(_SCOUT_TRAJECTORY)
        assert synthesized is not None
        text = render_synthesized_offer_text(synthesized, _SCOUT_TRAJECTORY, goal="do this\n```python\nx\n```")
        assert "\n```python\nx\n```" not in text

    def test_offer_text_steps_json_matches_synthesized_steps(self) -> None:
        synthesized = synthesize_code_block(_SCOUT_TRAJECTORY)
        assert synthesized is not None
        text = render_synthesized_offer_text(synthesized, _SCOUT_TRAJECTORY)
        rendered = json.dumps(synthesized.steps, indent=2, sort_keys=True)
        assert rendered in text


def _code_block_yaml(label: str) -> str:
    return (
        "workflow_definition:\n"
        "  blocks:\n"
        "    - block_type: code\n"
        f"      label: {label}\n"
        "      code: |\n"
        '        await page.goto("https://example.com/")\n'
    )


_SCOUT_TRAJECTORY = [
    {
        "tool_name": "type_text",
        "selector": "#search-box",
        "source_url": "https://example.com/",
        "typed_length": 5,
        "role": "textbox",
        "accessible_name": "Search",
    },
    {"tool_name": "press_key", "selector": "#search-box", "key": "Enter"},
    {
        "tool_name": "click",
        "selector": 'role=button[name="Add to cart"]',
        "role": "button",
        "accessible_name": "Add to cart",
    },
]


class TestSynthesizedArtifactMetadata:
    def test_skeleton_passes_the_validator_with_placeholders(self) -> None:
        # The skeleton passes the validator with only <fill> placeholders for the model-owned slots.
        metadata = build_synthesized_artifact_metadata(_SCOUT_TRAJECTORY)
        normalized, error = _normalize_code_artifact_metadata([metadata], _code_block_yaml(_SYNTHESIZED_BLOCK_LABEL))
        assert error is None
        assert list(normalized.keys()) == [_SYNTHESIZED_BLOCK_LABEL]

    def test_skeleton_never_asserts_satisfied_status(self) -> None:
        # The scout never ran+verified the authored block, so the only honest status is observed_not_verified.
        metadata = build_synthesized_artifact_metadata(_SCOUT_TRAJECTORY)
        statuses = [metadata["page_dependencies"][0]["status"], metadata["observation_refs"][0]["status"]]
        statuses += [claim["status"] for claim in metadata["claimed_outcomes"]]
        assert all(status == "observed_not_verified" for status in statuses)
        assert "satisfied" not in str(metadata)

    def test_skeleton_observation_ref_carries_scout_source_tool(self) -> None:
        metadata = build_synthesized_artifact_metadata(_SCOUT_TRAJECTORY)
        observation_ref = metadata["observation_refs"][0]
        assert observation_ref["source_tool"] == "scout_interaction"
        assert observation_ref["dependency_id"] == metadata["page_dependencies"][0]["id"]

    def test_skeleton_leaves_terminal_goal_for_the_model(self) -> None:
        metadata = build_synthesized_artifact_metadata(_SCOUT_TRAJECTORY)
        assert metadata["declared_goal"].startswith("<fill:")
        assert metadata["completion_criteria"][0]["text"].startswith("<fill:")
        assert metadata["claimed_outcomes"][0]["text"].startswith("<fill:")

    def test_skeleton_marks_placeholder_schema_self_authored(self) -> None:
        metadata = build_synthesized_artifact_metadata(_SCOUT_TRAJECTORY)
        assert metadata["claimed_outcomes"][0]["extraction_schema"].startswith("<fill:")
        assert metadata["claimed_outcomes"][0]["extraction_schema_provenance"] == "self_authored"

    def test_skeleton_is_byte_identical_per_trajectory(self) -> None:
        first = build_synthesized_artifact_metadata(_SCOUT_TRAJECTORY)
        second = build_synthesized_artifact_metadata(_SCOUT_TRAJECTORY)
        assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)

    def test_skeleton_omits_url_hint_when_no_source_url(self) -> None:
        metadata = build_synthesized_artifact_metadata([_interaction("press_key", key="Enter")])
        assert "url_hint" not in metadata["page_dependencies"][0]
        assert "current_url" not in metadata["observation_refs"][0]

    def test_offer_text_embeds_metadata_when_trajectory_supplied(self) -> None:
        synthesized = synthesize_code_block(_SCOUT_TRAJECTORY)
        assert synthesized is not None
        text = render_synthesized_offer_text(synthesized, _SCOUT_TRAJECTORY)
        assert "code_artifact_metadata" in text
        assert "```json" in text
        assert "scout_interaction" in text
        assert "returns every remaining violation at once" in text

    def test_offer_text_omits_metadata_without_trajectory(self) -> None:
        synthesized = synthesize_code_block(_SCOUT_TRAJECTORY)
        assert synthesized is not None
        text = render_synthesized_offer_text(synthesized)
        assert "code_artifact_metadata" not in text
        assert "observed_not_verified" not in text


class TestCredentialFillSynthesis:
    """A scouted fill_credential_field compiles into an attribute read on a
    credential-bound parameter — references only, never values."""

    def _credential_fill(self, **overrides: Any) -> dict[str, Any]:
        fields = {
            "selector": "#userName",
            "source_url": "https://authenticationtest.com/simpleFormAuth/",
            "typed_length": 24,
            "credential_id": "cred_123",
            "credential_field": "username",
            "credential_name": "authtest simple",
        }
        fields.update(overrides)
        return _interaction("fill_credential_field", **fields)

    def test_emits_attribute_fill_and_credential_parameter(self) -> None:
        result = synthesize_code_block([self._credential_fill()])
        assert result is not None
        assert 'await page.locator("#userName").fill(authtest_simple.username)' in result.code
        assert result.parameters == [{"key": "authtest_simple", "credential_id": "cred_123"}]

    def test_same_credential_shares_one_parameter(self) -> None:
        result = synthesize_code_block(
            [
                self._credential_fill(),
                self._credential_fill(selector="#passwordInput", credential_field="password", typed_length=12),
            ]
        )
        assert result is not None
        assert 'await page.locator("#userName").fill(authtest_simple.username)' in result.code
        assert 'await page.locator("#passwordInput").fill(authtest_simple.password)' in result.code
        assert result.parameters == [{"key": "authtest_simple", "credential_id": "cred_123"}]

    def test_totp_field_reads_runtime_otp_method(self) -> None:
        result = synthesize_code_block(
            [self._credential_fill(selector="#totpCode", credential_field="totp", typed_length=6)]
        )
        assert result is not None
        assert 'await page.locator("#totpCode").fill(await authtest_simple.otp())' in result.code

    def test_runtime_otp_fill_is_detected_as_credential_fill_code(self) -> None:
        assert code_contains_credential_fill('await page.locator("#otp").fill(await login_credential.otp())')

    def test_missing_credential_reference_is_dropped_with_note(self) -> None:
        result = synthesize_code_block(
            [
                self._credential_fill(credential_id=""),
                _interaction("click", selector="#next", source_url="https://example.com/login"),
            ]
        )
        assert result is not None
        assert ".fill(" not in result.code
        assert result.parameters == []
        assert any("credential" in note for note in result.notes)

    def test_unknown_credential_field_is_dropped(self) -> None:
        result = synthesize_code_block(
            [
                self._credential_fill(credential_field="cvv"),
                _interaction("click", selector="#next", source_url="https://example.com/login"),
            ]
        )
        assert result is not None
        assert ".fill(" not in result.code
        assert result.parameters == []

    def test_param_key_defaults_when_credential_name_missing(self) -> None:
        result = synthesize_code_block([self._credential_fill(credential_name="")])
        assert result is not None
        assert ".fill(credential.username)" in result.code
        assert result.parameters == [{"key": "credential", "credential_id": "cred_123"}]

    def test_credential_param_key_does_not_collide_with_typed_param(self) -> None:
        result = synthesize_code_block(
            [
                _interaction(
                    "type_text",
                    selector="#company",
                    source_url="https://example.com/form",
                    typed_length=6,
                    role="textbox",
                    accessible_name="authtest simple",
                ),
                self._credential_fill(),
            ]
        )
        assert result is not None
        assert result.parameters[0] == {"key": "authtest_simple"}
        assert result.parameters[1] == {"key": "authtest_simple_2", "credential_id": "cred_123"}
        assert ".fill(authtest_simple_2.username)" in result.code

    def test_offer_text_carries_credential_binding_contract(self) -> None:
        trajectory = [self._credential_fill()]
        synthesized = synthesize_code_block(trajectory)
        assert synthesized is not None
        text = render_synthesized_offer_text(synthesized, trajectory)
        assert "`authtest_simple` -> `cred_123`" in text
        assert "workflow_parameter_type: credential_id" in text
        assert "default_value" in text
        assert ".username` / `.password` attributes and `.otp()`" in text
        assert "authtest_simple" not in [p.get("key") for p in synthesized.parameters if "credential_id" not in p]

    def test_credential_parameters_excluded_from_plain_bind_line(self) -> None:
        trajectory = [
            _interaction(
                "type_text",
                selector="#q",
                source_url="https://example.com/",
                typed_length=4,
                role="textbox",
                accessible_name="Search",
            ),
            self._credential_fill(),
        ]
        synthesized = synthesize_code_block(trajectory)
        assert synthesized is not None
        text = render_synthesized_offer_text(synthesized, trajectory)
        assert "Workflow parameters referenced (bind these): search." in text
        assert "Credential parameters referenced" in text

    def test_plain_param_never_takes_a_bare_credential_field_name(self) -> None:
        # CodeBlock.execute injects a bound credential's fields under the bare
        # names username/password/totp, so a plain typed parameter must not
        # claim those keys or it would resolve to the secret value at runtime.
        result = synthesize_code_block(
            [
                _interaction(
                    "type_text",
                    selector="#confirm",
                    source_url="https://example.com/form",
                    typed_length=8,
                    role="textbox",
                    accessible_name="Password",
                ),
                self._credential_fill(),
            ]
        )
        assert result is not None
        assert result.parameters[0] == {"key": "password_field"}
        assert "fill(str(password_field))" in result.code
        assert {"key": "password"} not in result.parameters

    def test_synthesized_credential_code_is_valid_python(self) -> None:
        result = synthesize_code_block(
            [
                self._credential_fill(),
                self._credential_fill(selector="#passwordInput", credential_field="password"),
            ]
        )
        assert result is not None
        wrapped = "async def _block(page, authtest_simple):\n" + result.code
        ast.parse(wrapped)

    def test_synthesized_credential_code_passes_persist_safety_seam(self) -> None:
        result = synthesize_code_block(
            [
                self._credential_fill(),
                self._credential_fill(selector="#passwordInput", credential_field="password"),
            ]
        )
        assert result is not None
        workflow_yaml = (
            "title: Login with saved credential\n"
            "workflow_definition:\n"
            "  parameters:\n"
            "    - parameter_type: workflow\n"
            "      workflow_parameter_type: credential_id\n"
            "      key: authtest_simple\n"
            "      default_value: cred_123\n"
            "  blocks:\n"
            "    - block_type: code\n"
            "      label: login_with_saved_credential\n"
            "      parameter_keys:\n"
            "        - authtest_simple\n"
            "      code: |\n" + "\n".join(f"        {line}" for line in result.code.splitlines()) + "\n"
        )

        assert _code_block_safety_errors(workflow_yaml, None) == []


@pytest.mark.parametrize(
    ("code", "expected_codes"),
    [
        pytest.param(
            "await page.locator('button[type=submit]').first.click(timeout=5000)\n",
            (),
            id="valid-locator-click",
        ),
        pytest.param(
            "await page.locator('button[type=submit]').first().click(timeout=5000)\n",
            ("PLAYWRIGHT_API_MISMATCH",),
            id="locator-property-called-as-method",
        ),
    ],
)
def test_code_block_preflight_real_mypy_contract_matrix(
    real_mypy: None,
    code: str,
    expected_codes: tuple[str, ...],
) -> None:
    diagnostics = preflight_code_block(code)

    assert [diagnostic.code for diagnostic in diagnostics] == list(expected_codes)


def test_code_block_preflight_restores_recursion_limit(real_mypy: None) -> None:
    before = sys.getrecursionlimit()
    preflight_code_block("await page.locator('button[type=submit]').first.click(timeout=5000)\n")

    assert sys.getrecursionlimit() == before


class TestOfferDemonstratesStructuredReturn:
    def test_offer_directs_keyed_return_not_inner_text_blob(self) -> None:
        result = synthesize_code_block(
            [
                _interaction(
                    "click",
                    selector="#search-submit",
                    source_url="https://example.com/search",
                )
            ]
        )
        assert result is not None
        offer = render_synthesized_offer_text(
            result,
            trajectory=[
                {"tool_name": "click", "selector": "#search-submit", "source_url": "https://example.com/search"}
            ],
        )
        assert "keyed structure" in offer
        assert "inner_text" in offer
        assert 'return {"records":' in offer


_DOWNLOAD_SELECTOR = '[href="/files/report.pdf"]'


def _nav_click() -> dict[str, Any]:
    # The scout reaches the download page via a navigation click; the download affordance itself
    # is observed in nav_targets, so its selector is NOT this trajectory click.
    return _interaction("click", selector="div.stmt-row", source_url="https://example.com/bills")


def _download_target(**fields: Any) -> ReachedDownloadTarget:
    base: dict[str, Any] = {
        "selector": _DOWNLOAD_SELECTOR,
        "affordance_text": "Download PDF",
        "download_kind": "extension",
        "source_step": "trajectory_recency",
        "already_registered": False,
    }
    base.update(fields)
    return ReachedDownloadTarget(**base)


class TestDownloadRungSynthesis:
    def test_post_auth_resume_skips_login_prefix_without_download_target(self) -> None:
        result = synthesize_code_block(
            [
                _interaction(
                    CREDENTIAL_FILL_TOOL_NAME,
                    selector="#user",
                    source_url="https://example.com/bills",
                    credential_id="cred_123",
                    credential_name="mock_portal_login",
                    credential_field="username",
                ),
                _interaction("click", selector="#contBtn"),
                _interaction(
                    CREDENTIAL_FILL_TOOL_NAME,
                    selector="#pass",
                    credential_id="cred_123",
                    credential_name="mock_portal_login",
                    credential_field="password",
                ),
                _interaction("click", selector="#signinBtn"),
                _interaction("click", selector="#current-statement-row"),
            ],
        )
        assert result is not None
        lines = result.code.splitlines()
        assert lines[0] == "    _scout_entry_resume_after_auth = False"
        assert lines[1] == '    _scout_entry_target = page.locator("#user")'
        assert '        await page.goto("https://example.com/bills", wait_until="domcontentloaded")' in lines
        assert '            _scout_entry_resume_target = page.locator("#current-statement-row")' in lines
        assert "                _scout_entry_resume_after_auth = True" in lines
        assert "    if not _scout_entry_resume_after_auth:" in lines
        assert '        await page.locator("#user").fill(mock_portal_login.username)' in lines
        assert '    await page.locator("#current-statement-row").click()' in lines
        assert "_scout_entry_reused_current_page" not in result.code
        assert result.parameters == [{"key": "mock_portal_login", "credential_id": "cred_123"}]
        ast.parse("async def _block(page):\n" + result.code)

    def test_appended_terminal_step_compiled_from_typed_target(self, monkeypatch: pytest.MonkeyPatch) -> None:
        result = synthesize_code_block([_nav_click()], reached_download_target=_download_target())
        assert result is not None
        assert "_scout_entry_reused_current_page = False" in result.code
        assert 'await page.goto("https://example.com/bills", wait_until="domcontentloaded")' in result.code
        assert f"async with page.expect_download() as {_DOWNLOAD_VAR_BASE}:" in result.code
        download_obj = f"{_DOWNLOAD_VAR_BASE}_file"
        assert f"{download_obj} = await {_DOWNLOAD_VAR_BASE}.value" in result.code
        assert f"await {download_obj}.path()" in result.code
        assert '"downloaded_file_name": downloaded_file_name' in result.code
        assert '"download_url"' not in result.code
        assert '"downloaded_file_path"' not in result.code
        assert '"downloaded_files"' not in result.code
        # The execution-layer dir-diff registers the single landed file, so the synthesizer never save_as.
        assert "save_as" not in result.code
        # The click inside expect_download targets the TYPED download selector, not the navigation click.
        download_step = result.code.split("async with page.expect_download")[1]
        assert 'await page.locator("[href=\\"/files/report.pdf\\"]").click()' in download_step
        assert "div.stmt-row" not in download_step
        # A download does not navigate, so no trailing load-wait inside the appended step.
        assert 'wait_for_load_state("load")' not in download_step

    def test_already_registered_emits_no_download_step(self, monkeypatch: pytest.MonkeyPatch) -> None:
        result = synthesize_code_block(
            [_nav_click()], reached_download_target=_download_target(already_registered=True, selector="")
        )
        assert result is not None
        assert "expect_download" not in result.code

    def test_target_none_byte_identical_to_base(self, monkeypatch: pytest.MonkeyPatch) -> None:
        trajectory = [_nav_click()]
        base = synthesize_code_block(trajectory)
        none_target = synthesize_code_block(trajectory, reached_download_target=None)
        assert base is not None and none_target is not None
        assert base.code == none_target.code
        assert "expect_download" not in none_target.code

    def test_non_download_trajectory_emits_no_download_terminal(self) -> None:
        trajectory = [
            _interaction("type_text", selector="#user", source_url="https://example.com/", typed_value="abc"),
            _interaction("select_option", selector="#state", value="CA"),
            _interaction(
                "fill_credential_field",
                selector="#pw",
                credential_id="cred_123",
                credential_field="password",
                credential_name="Login",
            ),
            _interaction("press_key", selector="#user", key="Enter"),
            _interaction("click", selector="#submit", source_url="https://example.com/"),
        ]
        result = synthesize_code_block(trajectory)
        assert result is not None
        assert "expect_download" not in result.code

    def test_user_param_named_dl_info_is_renamed_via_reserved_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        result = synthesize_code_block(
            [
                _interaction(
                    "type_text",
                    selector="#field",
                    source_url="https://example.com/",
                    typed_value="x",
                    accessible_name=_DOWNLOAD_VAR_BASE,
                ),
                _nav_click(),
            ],
            reached_download_target=_download_target(),
        )
        assert result is not None
        param_keys = [p["key"] for p in result.parameters]
        assert _DOWNLOAD_VAR_BASE not in param_keys
        assert f"{_DOWNLOAD_VAR_BASE}_field" in param_keys
        assert f"async with page.expect_download() as {_DOWNLOAD_VAR_BASE}:" in result.code

    def test_emitted_download_snippet_is_safe_and_parses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        result = synthesize_code_block([_nav_click()], reached_download_target=_download_target())
        assert result is not None
        wrapped = "async def _block(page):\n" + result.code
        CodeBlock.is_safe_code(wrapped)
        assert not any(d.code == "SYNTAX_ERROR" for d in preflight_code_block(result.code, parameter_keys=()))
        ast.parse(wrapped)

    def test_download_snippet_awaits_completion_without_save_as(self, monkeypatch: pytest.MonkeyPatch) -> None:
        result = synthesize_code_block([_nav_click()], reached_download_target=_download_target())
        assert result is not None
        download_obj = f"{_DOWNLOAD_VAR_BASE}_file"
        assert result.code.count(f"{download_obj} = await {_DOWNLOAD_VAR_BASE}.value") == 1
        # Awaiting the path() completes the download into the run-scoped dir; the SKY-10937 dir-diff
        # registers the single file when available; the returned summary keeps the filename JSON-safe.
        assert f"await {download_obj}.path()" in result.code
        assert "return {" in result.code
        assert '"downloaded_file_name": downloaded_file_name' in result.code
        assert '"downloaded_file_path"' not in result.code
        assert '"download_url"' not in result.code
        assert '"downloaded_files"' not in result.code
        assert "save_as" not in result.code
        CodeBlock.is_safe_code("async def _block(page):\n" + result.code)

    def test_download_offer_text_only_present_for_download_snippet(self, monkeypatch: pytest.MonkeyPatch) -> None:
        download = synthesize_code_block([_nav_click()], reached_download_target=_download_target())
        plain = synthesize_code_block([_interaction("click", selector="#go", source_url="https://example.com/")])
        assert download is not None and plain is not None
        assert "expect_download" in render_synthesized_offer_text(download)
        assert "expect_download" not in render_synthesized_offer_text(plain)

    def test_non_live_call_sites_compile_without_kwarg(self, monkeypatch: pytest.MonkeyPatch) -> None:
        result = synthesize_code_block([_nav_click()])
        assert result is not None
        assert "expect_download" not in result.code


def _readonly_type(**overrides: Any) -> dict[str, Any]:
    base = _interaction(
        "type_text",
        selector="#electric-date",
        source_url="https://example.com/service",
        typed_length=10,
        role="textbox",
        accessible_name="Start date",
    )
    base.update(overrides)
    return base


def _deferred_conditional_snippet(code: str) -> str:
    lines = code.splitlines()
    start = next(i for i, ln in enumerate(lines) if f"if {_READONLY_DEFERRED_VAR} == " in ln)
    return textwrap.dedent("\n".join(lines[start : start + 4]))


def _guarded_deferred_snippet(code: str, guard_var: str) -> str:
    lines = code.splitlines()
    cond = [i for i, ln in enumerate(lines) if f"if {_READONLY_DEFERRED_VAR} == " in ln][-1]
    guard = max(i for i in range(cond) if lines[i].strip() == f"if not {guard_var}:")
    body = textwrap.dedent("\n".join(lines[cond : cond + 4]))
    return lines[guard].strip() + "\n" + textwrap.indent(body, _INDENT)


class TestReadonlyControlStateSynthesis:
    def test_readonly_holding_value_emits_nonraising_verify_not_fill(self) -> None:
        result = synthesize_code_block([_readonly_type(control_readonly=True, control_value_satisfied=True)])
        assert result is not None
        assert ".fill(str(start_date))" not in result.code
        assert 'await page.locator("#electric-date").input_value()' in result.code
        assert "!= str(start_date)" in result.code
        assert "raise AssertionError" not in result.code
        assert "print(" in result.code
        assert result.parameters == [{"key": "start_date"}]
        ast.parse("async def _block(page):\n" + result.code)

    def test_disabled_holding_value_emits_verify_not_fill(self) -> None:
        result = synthesize_code_block([_readonly_type(control_disabled=True, control_value_satisfied=True)])
        assert result is not None
        assert ".fill(str(" not in result.code
        assert ".input_value()" in result.code

    def test_readonly_needing_value_defers_assertion_after_later_picker(self) -> None:
        trajectory = [
            _readonly_type(control_readonly=True, control_value_satisfied=False),
            _interaction(
                "click",
                selector="#date-picker-next",
                source_url="https://example.com/service",
                role="button",
                accessible_name="Next",
            ),
        ]
        result = synthesize_code_block(trajectory)
        assert result is not None
        code = result.code
        assert ".fill(str(start_date))" not in code
        assert "raise AssertionError" in code
        assert code.index('await page.locator("#date-picker-next").click()') < code.index("raise AssertionError")
        assert code.index(f"{_READONLY_DEFERRED_VAR} = await") > code.index(".click()")
        ast.parse("async def _block(page):\n" + code)

    def test_readonly_needing_value_still_asserts_without_actuator(self) -> None:
        result = synthesize_code_block([_readonly_type(control_readonly=True, control_value_satisfied=False)])
        assert result is not None
        assert "raise AssertionError" in result.code
        assert ".fill(str(" not in result.code
        ast.parse("async def _block(page):\n" + result.code)

    def test_post_auth_resume_header_always_carries_a_body(self) -> None:
        # The resume-only entry header (elif entry_post_auth_resume_index) gets a guarding `pass` so that a
        # pre-resume body reduced to deferring readonly actions never compiles to an empty block (SKY-12102).
        trajectory = [
            _interaction(
                CREDENTIAL_FILL_TOOL_NAME,
                selector="#user",
                source_url="https://example.com/service",
                credential_id="cred_1",
                credential_name="mock_login",
                credential_field="username",
            ),
            _interaction("click", selector="#signin", source_url="https://example.com/service"),
            _readonly_type(control_readonly=True, control_value_satisfied=False),
            _interaction("click", selector="#statement", source_url="https://example.com/service"),
        ]
        result = synthesize_code_block(trajectory)
        assert result is not None
        lines = result.code.splitlines()
        header_index = lines.index("    if not _scout_entry_resume_after_auth:")
        assert lines[header_index + 1] == f"{_INDENT * 2}pass"
        ast.parse("async def _block(page):\n" + result.code)

    def test_deferred_assertion_short_circuits_with_replayed_trajectory_on_reuse(self) -> None:
        trajectory = [
            _readonly_type(control_readonly=True, control_value_satisfied=False),
            _interaction(
                "click",
                selector="#date-picker-next",
                source_url="https://example.com/service",
                role="button",
                accessible_name="Next",
            ),
        ]
        result = synthesize_code_block(trajectory, reached_download_target=_download_target())
        assert result is not None
        lines = result.code.splitlines()
        guard = next(ln for ln in lines if ln.strip() == f"if not {_ENTRY_REUSED_VAR}:")
        guard_indent = len(guard) - len(guard.lstrip())
        read_idx = next(i for i, ln in enumerate(lines) if f"{_READONLY_DEFERRED_VAR} = await" in ln)
        deferred_try = max(i for i in range(read_idx) if lines[i].strip() == "try:")
        assert (len(lines[deferred_try]) - len(lines[deferred_try].lstrip())) > guard_indent
        raise_line = next(ln for ln in lines if "raise AssertionError" in ln)
        assert (len(raise_line) - len(raise_line.lstrip())) > guard_indent
        ast.parse("async def _block(page):\n" + result.code)

    def test_unknown_editability_falls_through_to_fill(self) -> None:
        result = synthesize_code_block([_readonly_type()])
        assert result is not None
        assert 'await page.locator("#electric-date").fill(str(start_date))' in result.code
        assert ".input_value()" not in result.code
        assert "raise AssertionError" not in result.code

    def test_editable_control_state_falls_through_to_fill(self) -> None:
        result = synthesize_code_block([_readonly_type(control_readonly=False, control_disabled=False)])
        assert result is not None
        assert "fill(str(start_date))" in result.code
        assert ".input_value()" not in result.code

    def test_deferred_verify_var_is_cleaned_up_not_leaked_as_output(self) -> None:
        result = synthesize_code_block([_readonly_type(control_readonly=True, control_value_satisfied=False)])
        assert result is not None
        assert f"del {_READONLY_DEFERRED_VAR}" in result.code

    def test_readonly_verify_matches_fill_param_registration(self) -> None:
        readonly = synthesize_code_block(
            [_readonly_type(control_readonly=True, control_value_satisfied=True, typed_value="example-value")]
        )
        editable = synthesize_code_block([_readonly_type(typed_value="example-value")])
        assert readonly is not None and editable is not None
        assert readonly.parameters == editable.parameters

    def test_deferred_empty_read_raises_honest_fail(self) -> None:
        result = synthesize_code_block([_readonly_type(control_readonly=True, control_value_satisfied=False)])
        assert result is not None
        snippet = _deferred_conditional_snippet(result.code)
        with pytest.raises(AssertionError):
            exec(snippet, {"_scout_readonly_actual": "", "start_date": "06/22/2026"})

    def test_deferred_reformatted_nonempty_read_prints_not_raises(self, capsys: pytest.CaptureFixture[str]) -> None:
        result = synthesize_code_block([_readonly_type(control_readonly=True, control_value_satisfied=False)])
        assert result is not None
        snippet = _deferred_conditional_snippet(result.code)
        exec(snippet, {"_scout_readonly_actual": "2026-06-22", "start_date": "06/22/2026"})
        assert "does not match expected" in capsys.readouterr().out

    def test_deferred_matching_nonempty_read_neither_raises_nor_prints(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        result = synthesize_code_block([_readonly_type(control_readonly=True, control_value_satisfied=False)])
        assert result is not None
        snippet = _deferred_conditional_snippet(result.code)
        exec(snippet, {"_scout_readonly_actual": "06/22/2026", "start_date": "06/22/2026"})
        assert capsys.readouterr().out == ""

    def test_deferred_unreadable_none_read_is_silent(self, capsys: pytest.CaptureFixture[str]) -> None:
        result = synthesize_code_block([_readonly_type(control_readonly=True, control_value_satisfied=False)])
        assert result is not None
        snippet = _deferred_conditional_snippet(result.code)
        exec(snippet, {"_scout_readonly_actual": None, "start_date": "06/22/2026"})
        assert capsys.readouterr().out == ""

    def test_resume_gating_partitions_pre_and_post_resume_deferred_assertions(self) -> None:
        trajectory = [
            _readonly_type(control_readonly=True, control_value_satisfied=False, selector="#account-id"),
            _interaction(
                CREDENTIAL_FILL_TOOL_NAME,
                selector="#user",
                source_url="https://example.com/login",
                credential_id="cred_123",
                credential_name="mock_portal_login",
                credential_field="username",
            ),
            _interaction("click", selector="#signinBtn", source_url="https://example.com/login"),
            _interaction("click", selector="#current-statement-row", source_url="https://example.com/bills"),
            _readonly_type(
                control_readonly=True,
                control_value_satisfied=False,
                selector="#post-field",
                source_url="https://example.com/bills",
            ),
        ]
        result = synthesize_code_block(trajectory)
        assert result is not None
        lines = result.code.splitlines()
        pre_read = next(i for i, ln in enumerate(lines) if "#account-id" in ln and ".input_value()" in ln)
        post_read = next(i for i, ln in enumerate(lines) if "#post-field" in ln and ".input_value()" in ln)
        guard = next(
            i
            for i, ln in enumerate(lines)
            if ln == f"{_INDENT}if not {_ENTRY_RESUME_AFTER_AUTH_VAR}:" and i > post_read
        )
        assert post_read < guard < pre_read
        guard_indent = len(lines[guard]) - len(lines[guard].lstrip())
        assert (len(lines[pre_read]) - len(lines[pre_read].lstrip())) > guard_indent
        ast.parse("async def _block(page):\n" + result.code)

    def test_resume_and_reuse_gates_short_circuit_deferred_assertion_at_runtime(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        resume = synthesize_code_block(
            [
                _readonly_type(control_readonly=True, control_value_satisfied=False, selector="#account-id"),
                _interaction(
                    CREDENTIAL_FILL_TOOL_NAME,
                    selector="#user",
                    source_url="https://example.com/login",
                    credential_id="cred_123",
                    credential_name="mock_portal_login",
                    credential_field="username",
                ),
                _interaction("click", selector="#signinBtn", source_url="https://example.com/login"),
                _interaction("click", selector="#current-statement-row", source_url="https://example.com/bills"),
                _readonly_type(
                    control_readonly=True,
                    control_value_satisfied=False,
                    selector="#post-field",
                    source_url="https://example.com/bills",
                ),
            ]
        )
        assert resume is not None
        pre_resume = _guarded_deferred_snippet(resume.code, _ENTRY_RESUME_AFTER_AUTH_VAR)
        post_resume = _deferred_conditional_snippet(resume.code)

        exec(
            pre_resume,
            {_ENTRY_RESUME_AFTER_AUTH_VAR: True, _READONLY_DEFERRED_VAR: "2026-06-22", "start_date": "06/22/2026"},
        )
        assert capsys.readouterr().out == ""
        with pytest.raises(AssertionError):
            exec(pre_resume, {_ENTRY_RESUME_AFTER_AUTH_VAR: False, _READONLY_DEFERRED_VAR: ""})
        with pytest.raises(AssertionError):
            exec(post_resume, {_ENTRY_RESUME_AFTER_AUTH_VAR: True, _READONLY_DEFERRED_VAR: ""})

        reuse = synthesize_code_block(
            [
                _readonly_type(control_readonly=True, control_value_satisfied=False),
                _interaction(
                    "click",
                    selector="#date-picker-next",
                    source_url="https://example.com/service",
                    role="button",
                    accessible_name="Next",
                ),
            ],
            reached_download_target=_download_target(),
        )
        assert reuse is not None
        reuse_gated = _guarded_deferred_snippet(reuse.code, _ENTRY_REUSED_VAR)
        exec(reuse_gated, {_ENTRY_REUSED_VAR: True, _READONLY_DEFERRED_VAR: "2026-06-22", "start_date": "06/22/2026"})
        assert capsys.readouterr().out == ""
        with pytest.raises(AssertionError):
            exec(reuse_gated, {_ENTRY_REUSED_VAR: False, _READONLY_DEFERRED_VAR: ""})


class TestReadonlyControlStateCarry:
    def test_fill_carry_roundtrip_preserves_control_state_for_type_text(self) -> None:
        interaction = _readonly_type(control_readonly=True, control_value_satisfied=True)
        carry = _fill_carry_from_scout_trajectory([interaction])
        assert len(carry) == 1
        assert carry[0].control_readonly is True
        assert carry[0].control_value_satisfied is True
        rebound = _fill_carry_to_interaction(carry[0], trajectory_index=0)
        assert rebound["control_readonly"] is True
        assert rebound["control_value_satisfied"] is True
        result = synthesize_code_block([rebound])
        assert result is not None
        assert ".fill(str(" not in result.code
        assert ".input_value()" in result.code

    def test_credential_carry_persists_no_control_state_keys(self) -> None:
        credential_carry = FillCarry(
            source_url="https://example.com/login",
            selector="#password",
            tool_name="fill_credential_field",
            credential_id="cred_example",
            credential_field="password",
        )
        persisted = StructuredContext(fill_carry=[credential_carry]).to_json_str()
        assert "control_readonly" not in persisted
        assert "control_disabled" not in persisted
        assert "control_value_satisfied" not in persisted

    def test_type_text_carry_persists_control_value_satisfied_bool(self) -> None:
        interaction = _readonly_type(control_readonly=True, control_value_satisfied=False)
        carry = _fill_carry_from_scout_trajectory([interaction])
        persisted = StructuredContext(fill_carry=carry).to_json_str()
        assert '"control_readonly": true' in persisted
        assert '"control_value_satisfied": false' in persisted


class TestEmittedInteractionPartition:
    def _mixed_trajectory(self) -> list[dict[str, Any]]:
        return [
            {"tool_name": "click", "selector": "#open", "source_url": "https://example.com/start"},
            {
                "tool_name": "type_text",
                "selector": "#name",
                "typed_value": "Ada",
                "role": "textbox",
                "accessible_name": "Name",
            },
            {"tool_name": "hover", "selector": "#menu"},
            {"tool_name": "select_option", "selector": "#state"},
            {
                "tool_name": "type_text",
                "selector": "#locked",
                "typed_value": "fixed",
                "control_readonly": True,
                "control_value_satisfied": True,
            },
            {
                "tool_name": "click",
                "selector": "#accept",
                "role": "button",
                "accessible_name": "Accept all cookies",
            },
            {"tool_name": "press_key", "selector": "#name", "key": "Enter"},
        ]

    def test_every_retained_index_in_exactly_one_partition(self) -> None:
        trajectory = self._mixed_trajectory()

        result = synthesize_code_block(trajectory, strict_selectors=True)

        assert result is not None
        diagnostics = result.diagnostics
        assert diagnostics.truncated is False
        emitted = {record["trajectory_index"] for record in diagnostics.emitted_interactions}
        dropped = {record["trajectory_index"] for record in diagnostics.dropped_interactions}
        forgiven = {record["trajectory_index"] for record in diagnostics.forgiven_interactions}
        assert emitted | dropped | forgiven == set(range(len(trajectory)))
        assert emitted & dropped == set()
        assert emitted & forgiven == set()
        assert dropped & forgiven == set()

    def test_emitted_records_carry_method_selector_and_lane_flags(self) -> None:
        trajectory = self._mixed_trajectory()

        result = synthesize_code_block(trajectory, strict_selectors=True)

        assert result is not None
        by_index = {record["trajectory_index"]: record for record in result.diagnostics.emitted_interactions}
        assert by_index[0]["method"] == "click"
        assert by_index[0]["selector"] == "#open"
        assert "lane" not in by_index[0]
        assert by_index[1]["method"] == "fill"
        assert by_index[4]["method"] == "input_value"
        assert by_index[4]["lane"] == "readonly_skip"
        assert by_index[5]["method"] == "click"
        assert by_index[5]["lane"] == "optional_dismissal"
        assert by_index[6]["method"] == "press"
        dropped_reasons = {
            record["trajectory_index"]: record["reason_code"] for record in result.diagnostics.dropped_interactions
        }
        assert dropped_reasons[2] == "unsupported_tool"
        assert dropped_reasons[3] == "missing_value"

    def test_every_emitted_record_carries_verbatim_call_source(self) -> None:
        trajectory = self._mixed_trajectory()

        result = synthesize_code_block(trajectory, strict_selectors=True)

        assert result is not None
        assert result.diagnostics.emitted_interactions
        for record in result.diagnostics.emitted_interactions:
            call_source = str(record.get("call_source") or "")
            assert call_source.strip()
            for line in call_source.splitlines():
                assert line.strip() in result.code
        by_index = {record["trajectory_index"]: record for record in result.diagnostics.emitted_interactions}
        assert 'await page.locator("#open").click()' in by_index[0]["call_source"]

    def test_entry_replay_prefix_indices_are_forgiven_not_lost(self) -> None:
        trajectory = [
            {"tool_name": "click", "selector": "button", "source_url": "https://example.com/start"},
            {"tool_name": "click", "selector": "#promo"},
            {
                "tool_name": "type_text",
                "selector": "#name",
                "typed_value": "Ada",
                "role": "textbox",
                "accessible_name": "Name",
            },
            {"tool_name": "click", "selector": "#submit"},
        ]

        result = synthesize_code_block(trajectory, strict_selectors=True)

        assert result is not None
        diagnostics = result.diagnostics
        emitted = {record["trajectory_index"] for record in diagnostics.emitted_interactions}
        dropped = {record["trajectory_index"] for record in diagnostics.dropped_interactions}
        forgiven = {record["trajectory_index"] for record in diagnostics.forgiven_interactions}
        assert emitted | dropped | forgiven == set(range(len(trajectory)))
        assert emitted == {2, 3}
        assert dropped == {0}
        assert diagnostics.forgiven_interactions == [
            {"trajectory_index": 1, "tool_name": "click", "lane": "entry_replay_prefix"}
        ]


def _covering_draft_calls(diagnostics: SynthesisDiagnostics) -> list[tuple[str, str]]:
    return [
        (str(record.get("method") or ""), str(record.get("locator") or ""))
        for record in diagnostics.emitted_interactions
        if not str(record.get("lane") or "")
    ]


class TestSpinePartitionFindings:
    def _spine_trajectory(self) -> list[dict[str, Any]]:
        return [
            {"tool_name": "click", "selector": "#stage-a", "source_url": "https://example.com/records"},
            {"tool_name": "click", "selector": "#stage-b"},
        ]

    def test_retained_manifest_covers_every_partition_index(self) -> None:
        result = synthesize_code_block(self._spine_trajectory(), strict_selectors=True)
        assert result is not None
        diagnostics = result.diagnostics
        assert diagnostics.retained_trajectory_indices == list(range(2))
        recorded = (
            {record["trajectory_index"] for record in diagnostics.emitted_interactions}
            | {record["trajectory_index"] for record in diagnostics.dropped_interactions}
            | {record["trajectory_index"] for record in diagnostics.forgiven_interactions}
        )
        assert set(diagnostics.retained_trajectory_indices) == recorded

    def test_covered_draft_has_no_findings(self) -> None:
        result = synthesize_code_block(self._spine_trajectory(), strict_selectors=True)
        assert result is not None
        draft_calls = _covering_draft_calls(result.diagnostics)
        assert spine_partition_findings(result.diagnostics, draft_calls, self._spine_trajectory()) == []

    def test_unforgiven_drop_is_a_typed_finding(self) -> None:
        trajectory = [
            {"tool_name": "click", "selector": "#stage-a", "source_url": "https://example.com/records"},
            {"tool_name": "press_key", "key": ""},
        ]
        result = synthesize_code_block(trajectory, strict_selectors=True)
        assert result is not None
        draft_calls = _covering_draft_calls(result.diagnostics)
        findings = spine_partition_findings(result.diagnostics, draft_calls, trajectory)
        kinds = {finding.kind for finding in findings}
        assert UNFORGIVEN_DROP_FINDING in kinds
        drop_finding = next(finding for finding in findings if finding.kind == UNFORGIVEN_DROP_FINDING)
        assert obligation_finding_reason_code(drop_finding) == SCOUTED_SPINE_DROPPED_UNFORGIVEN_REASON_CODE

    def test_entry_opener_drop_is_forgiven_not_flagged(self) -> None:
        trajectory = [
            {
                "tool_name": "click",
                "selector": "button",
                "role": "button",
                "accessible_name": "Open menu",
                "source_url": "https://example.com/records",
            },
            {"tool_name": "click", "selector": "#stage-b"},
        ]
        result = synthesize_code_block(trajectory, strict_selectors=True)
        assert result is not None
        draft_calls = _covering_draft_calls(result.diagnostics)
        findings = spine_partition_findings(result.diagnostics, draft_calls, trajectory)
        assert all(finding.kind != UNFORGIVEN_DROP_FINDING for finding in findings)

    def test_truncation_leaves_post_break_indices_in_manifest_only(self) -> None:
        trajectory = [
            {"tool_name": "click", "selector": f"#stage-{index}", "source_url": "https://example.com/records"}
            for index in range(_MAX_STEPS + 3)
        ]
        result = synthesize_code_block(trajectory, strict_selectors=True)
        assert result is not None
        diagnostics = result.diagnostics
        assert diagnostics.truncated is True
        assert diagnostics.retained_trajectory_indices == list(range(len(trajectory)))
        recorded = {record["trajectory_index"] for record in diagnostics.emitted_interactions}
        post_break = set(diagnostics.retained_trajectory_indices) - recorded
        assert post_break
        findings = spine_partition_findings(diagnostics, _covering_draft_calls(diagnostics), trajectory)
        kinds = {finding.kind for finding in findings}
        assert TRUNCATED_FINDING in kinds
        assert UNRECORDED_INDEX_FINDING in kinds
        truncated_finding = next(finding for finding in findings if finding.kind == TRUNCATED_FINDING)
        assert obligation_finding_reason_code(truncated_finding) == SCOUTED_SPINE_TRUNCATED_REASON_CODE


_STRUCTURAL_BUTTON_XPATH = 'xpath=/*[name()="body"][1]/*[name()="button"][3]'


class TestTerminalDismissalReclassification:
    def test_anonymous_structural_terminal_click_after_required_work_is_required(self) -> None:
        trajectory = [
            {"tool_name": "click", "selector": "#stage-a", "source_url": "https://example.com/records"},
            {"tool_name": "click", "selector": _STRUCTURAL_BUTTON_XPATH},
        ]
        result = synthesize_code_block(trajectory, strict_selectors=True)
        assert result is not None
        assert "_scout_optional_dismissal" not in result.code
        assert "button:has-text('Accept')" not in result.code
        by_index = {record["trajectory_index"]: record for record in result.diagnostics.emitted_interactions}
        assert not str(by_index[1].get("lane") or "")

    def test_named_terminal_dismissal_keeps_the_optional_lane(self) -> None:
        trajectory = [
            {"tool_name": "click", "selector": "#stage-a", "source_url": "https://example.com/records"},
            {
                "tool_name": "click",
                "selector": _STRUCTURAL_BUTTON_XPATH,
                "role": "button",
                "accessible_name": "Accept all cookies",
            },
        ]
        result = synthesize_code_block(trajectory, strict_selectors=True)
        assert result is not None
        assert "_scout_optional_dismissal" in result.code
        by_index = {record["trajectory_index"]: record for record in result.diagnostics.emitted_interactions}
        assert by_index[1].get("lane") == "optional_dismissal"

    def test_dismissal_only_single_step_keeps_the_optional_lane(self) -> None:
        trajectory = [
            {"tool_name": "click", "selector": _STRUCTURAL_BUTTON_XPATH, "source_url": "https://example.com/records"},
        ]
        result = synthesize_code_block(trajectory, strict_selectors=True)
        assert result is not None
        assert "_scout_optional_dismissal" in result.code


class TestUncoveredRequiredEmittedInteractions:
    def _emitted(self) -> list[dict[str, Any]]:
        return [
            {"method": "click", "locator": 'page.locator("#a")', "selector": "#a"},
            {"method": "fill", "locator": 'page.locator("#b")', "selector": "#b"},
            {"method": "click", "locator": 'page.locator("#c")', "selector": "#c"},
        ]

    def test_verbatim_in_order_resubmission_clears(self) -> None:
        draft_calls = [
            ("click", 'page.locator("#a")'),
            ("fill", 'page.locator("#b")'),
            ("click", 'page.locator("#c")'),
        ]

        assert uncovered_required_emitted_interactions(self._emitted(), draft_calls) == []

    def test_reordered_but_complete_draft_reports_under_build(self) -> None:
        draft_calls = [
            ("fill", 'page.locator("#b")'),
            ("click", 'page.locator("#a")'),
            ("click", 'page.locator("#c")'),
        ]

        uncovered = uncovered_required_emitted_interactions(self._emitted(), draft_calls)

        assert [record["selector"] for record in uncovered] == ["#b", "#c"]

    def test_first_miss_over_reports_later_rungs_as_missing(self) -> None:
        draft_calls = [
            ("click", 'page.locator("#a")'),
            ("click", 'page.locator("#c")'),
        ]

        uncovered = uncovered_required_emitted_interactions(self._emitted(), draft_calls)

        assert [record["selector"] for record in uncovered] == ["#b", "#c"]

    def test_shared_name_literal_on_different_element_does_not_cover(self) -> None:
        emitted = [
            {"method": "click", "locator": 'page.get_by_role("button", name="Submit")', "selector": "Submit"},
        ]
        draft_calls = [("click", 'page.get_by_role("link", name="Submit")')]

        uncovered = uncovered_required_emitted_interactions(emitted, draft_calls)

        assert [record["selector"] for record in uncovered] == ["Submit"]

    def test_bare_locator_call_with_exact_full_selector_covers(self) -> None:
        emitted = [
            {"method": "click", "locator": 'page.locator("#a").first', "selector": "#a"},
        ]
        draft_calls = [("click", 'page.locator("#a")')]

        assert uncovered_required_emitted_interactions(emitted, draft_calls) == []

    def test_receiver_quoting_selector_among_other_literals_does_not_cover(self) -> None:
        emitted = [
            {"method": "click", "locator": 'page.locator("#a")', "selector": "#a"},
        ]
        draft_calls = [("click", 'page.locator("#wrapper").locator("#a", has_text="Go")')]

        uncovered = uncovered_required_emitted_interactions(emitted, draft_calls)

        assert [record["selector"] for record in uncovered] == ["#a"]

    def test_lane_flagged_records_are_not_required(self) -> None:
        emitted = self._emitted()
        emitted[1]["lane"] = "optional_dismissal"
        draft_calls = [
            ("click", 'page.locator("#a")'),
            ("click", 'page.locator("#c")'),
        ]

        assert uncovered_required_emitted_interactions(emitted, draft_calls) == []


_BILLS_URL = "https://example.com/bills"
_STATEMENT_URL = "https://example.com/bills/statement"


def _anchored_trajectory() -> list[dict[str, Any]]:
    return [
        _interaction("type_text", selector="#account", source_url=_BILLS_URL, typed_value="A-1", trajectory_index=0),
        _interaction("click", selector="#statement-row", source_url=_BILLS_URL, trajectory_index=1),
        _interaction("click", selector="#view-printable", source_url=_STATEMENT_URL, trajectory_index=2),
    ]


class TestDownloadTerminalSequencing:
    def test_post_capture_navigation_is_dropped_and_terminal_is_last(self) -> None:
        result = synthesize_code_block(
            _anchored_trajectory(),
            reached_download_target=_download_target(trajectory_anchor=1),
        )

        assert result is not None
        assert '"#view-printable"' not in result.code
        assert '"#statement-row"' in result.code
        assert result.code.index("async with page.expect_download()") > result.code.index('"#statement-row"')
        assert '"downloaded_file_name"' in result.code
        assert result.diagnostics.download_terminal_anchor == 1
        assert result.diagnostics.download_terminal_dropped_trailing == 1
        assert [record["selector"] for record in result.diagnostics.emitted_interactions] == [
            "#account",
            "#statement-row",
        ]

    def test_unanchored_target_keeps_the_whole_trajectory(self) -> None:
        result = synthesize_code_block(
            _anchored_trajectory(),
            reached_download_target=_download_target(),
        )

        assert result is not None
        assert '"#view-printable"' in result.code
        assert result.diagnostics.download_terminal_dropped_trailing == 0

    def test_anchor_at_the_last_interaction_is_byte_identical_to_unanchored(self) -> None:
        anchored = synthesize_code_block(
            _anchored_trajectory(),
            reached_download_target=_download_target(trajectory_anchor=2),
        )
        unanchored = synthesize_code_block(
            _anchored_trajectory(),
            reached_download_target=_download_target(),
        )

        assert anchored is not None and unanchored is not None
        assert anchored.code == unanchored.code
        assert anchored.diagnostics.download_terminal_dropped_trailing == 0

    def test_anchor_survives_trajectory_eviction(self) -> None:
        evicted = [
            _interaction("click", selector="#statement-row", source_url=_BILLS_URL, trajectory_index=5),
            _interaction("click", selector="#view-printable", source_url=_STATEMENT_URL, trajectory_index=6),
        ]

        result = synthesize_code_block(evicted, reached_download_target=_download_target(trajectory_anchor=5))

        assert result is not None
        assert '"#view-printable"' not in result.code
        assert result.diagnostics.download_terminal_dropped_trailing == 1

    def test_registered_target_is_never_sequenced(self) -> None:
        result = synthesize_code_block(
            _anchored_trajectory(),
            reached_download_target=_download_target(
                already_registered=True, download_kind="registered", selector="", trajectory_anchor=1
            ),
        )

        assert result is not None
        assert '"#view-printable"' in result.code
        assert "expect_download" not in result.code

    def test_extraction_suffix_composes_after_the_sequenced_terminal(self) -> None:
        trajectory = [
            _interaction("click", selector="#show-details", source_url=_BILLS_URL, trajectory_index=0),
            _interaction("click", selector="#view-printable", source_url=_STATEMENT_URL, trajectory_index=1),
        ]

        result = synthesize_code_block_with_extraction(
            trajectory,
            _extraction_plan(),
            reached_download_target=_download_target(trajectory_anchor=0),
        )

        assert result is not None
        assert '"#view-printable"' not in result.interaction_code
        assert "async with page.expect_download()" in result.interaction_code
        assert result.extraction_code
        assert result.code.startswith(result.interaction_code)

    def test_capture_stamps_the_latest_trajectory_index(self) -> None:
        ctx = SimpleNamespace(scout_trajectory=_anchored_trajectory()[:2])

        stamped = _with_trajectory_anchor(ctx, _download_target())  # type: ignore[arg-type]

        assert stamped.trajectory_anchor == 1
        assert (
            _with_trajectory_anchor(SimpleNamespace(scout_trajectory=[]), _download_target()).trajectory_anchor is None
        )  # type: ignore[arg-type]


_STATEMENT_SELECTOR = "a[href='/statements/100245_2026-05.pdf']"
_DECLARED = {"account_number": "100245", "billing_period": "May 2026"}


def _witnessed_click(*, selector: str = _STATEMENT_SELECTOR, accessible_name: str = "Download May") -> dict[str, Any]:
    interaction: dict[str, Any] = {
        "tool_name": "click",
        "selector": selector,
        "accessible_name": accessible_name,
        "role": "link",
        "source_url": "https://example.com/statements",
    }
    correspondences = input_correspondences_for_interaction(interaction, _DECLARED)
    if correspondences:
        interaction["input_correspondences"] = correspondences
    return interaction


def test_input_correspondence_selector_identity_and_month() -> None:
    correspondences = input_correspondences_for_interaction(
        {"tool_name": "click", "selector": _STATEMENT_SELECTOR}, _DECLARED
    )
    by_key = {c["input_key"]: c for c in correspondences}
    assert by_key["account_number"] == {
        "input_key": "account_number",
        "matched_literal": "100245",
        "parameter_value": "100245",
        "surface": "selector",
        "transform": "identity",
        "position": 20,
    }
    assert by_key["billing_period"]["matched_literal"] == "2026-05"
    assert by_key["billing_period"]["parameter_value"] == "May 2026"
    assert by_key["billing_period"]["transform"] == "month_name_to_iso"


def test_templated_hole_uses_validated_span_not_earlier_substring() -> None:
    # "Widget" also occurs as a non-boundary substring inside "Widgetry"; the hole must template the
    # boundary-validated standalone span, not the first find() hit.
    selector = 'a[aria-label="Widgetry Widget"]'
    holes = input_correspondences_for_interaction({"tool_name": "click", "selector": selector}, {"gadget": "Widget"})
    assert [h["position"] for h in holes] == [23]
    expr = build_input_templated_locator(surface="selector", selector=selector, role="", name="", holes=holes)
    assert expr is not None
    assert "Widgetry {gadget}" in expr
    assert "{gadget}ry" not in expr


@pytest.mark.parametrize(
    "declared",
    [
        {"account_number": "24"},
        {"account_number": "10 0245"},
        {"account_number": "100245 "},
        {"class": "100245"},
        {"page": "100245"},
        {"re": "100245"},
        {"_scout_month_to_iso": "100245"},
        {"account_number": "100245'] , [href"},
    ],
)
def test_input_correspondence_rejects_unsafe_or_unnamed(declared: dict[str, str]) -> None:
    assert (
        input_correspondences_for_interaction({"tool_name": "click", "selector": _STATEMENT_SELECTOR}, declared) == []
    )


def test_input_correspondence_ignores_non_click() -> None:
    assert (
        input_correspondences_for_interaction({"tool_name": "type_text", "selector": _STATEMENT_SELECTOR}, _DECLARED)
        == []
    )


def test_input_correspondence_requires_quoted_segment() -> None:
    # `100245` appears only in an unquoted structural position, never inside a quoted attribute value.
    assert (
        input_correspondences_for_interaction(
            {"tool_name": "click", "selector": "div.row-100245 > a"}, {"account_number": "100245"}
        )
        == []
    )


def test_input_correspondence_rejects_second_occurrence() -> None:
    selector = "a[href='/x/100245'][data-id='100245']"
    assert (
        input_correspondences_for_interaction(
            {"tool_name": "click", "selector": selector}, {"account_number": "100245"}
        )
        == []
    )


def test_synthesize_templated_selector_identical_across_modes() -> None:
    trajectory = [_witnessed_click()]
    non_strict = synthesize_code_block(list(trajectory), strict_selectors=False)
    strict = synthesize_code_block(list(trajectory), strict_selectors=True)
    assert non_strict is not None and strict is not None
    templated = "page.locator(f\"a[href='/statements/{account_number}_{_scout_month_to_iso(billing_period)}.pdf']\")"
    assert f"await {templated}.click()" in non_strict.code
    assert f"await {templated}.click()" in strict.code


def test_synthesize_mints_one_witness_row_per_key() -> None:
    trajectory = [_witnessed_click(), _witnessed_click(selector=_STATEMENT_SELECTOR, accessible_name="View May")]
    synthesized = synthesize_code_block(trajectory)
    assert synthesized is not None
    witness_rows = [p for p in synthesized.parameters if p.get("source") == LOCATOR_WITNESS_PARAM_SOURCE]
    assert sorted((row["key"], row["default_value"]) for row in witness_rows) == [
        ("account_number", "100245"),
        ("billing_period", "May 2026"),
    ]


def test_synthesize_prelude_before_entry_and_preflight_clean() -> None:
    synthesized = synthesize_code_block([_witnessed_click()])
    assert synthesized is not None
    code_lines = synthesized.code.splitlines()
    guard_line = next(i for i, line in enumerate(code_lines) if "invalid value for grounded parameter" in line)
    entry_line = next(i for i, line in enumerate(code_lines) if "_scout_entry_target =" in line)
    assert guard_line < entry_line
    diagnostics = preflight_code_block(synthesized.code, parameter_keys=["account_number", "billing_period"])
    assert not any(diagnostic.code == "SANDBOX_UNRESOLVED_NAME" for diagnostic in diagnostics)


def test_templated_locator_reads_declared_input_at_runtime() -> None:
    plan_holes = input_correspondences_for_interaction(
        {"tool_name": "click", "selector": _STATEMENT_SELECTOR}, _DECLARED
    )
    expr = build_input_templated_locator(
        surface="selector", selector=_STATEMENT_SELECTOR, role="", name="", holes=plan_holes
    )
    assert expr is not None

    class _Page:
        def locator(self, value: str) -> str:
            return value

    source_lines = ["def _block(page, account_number, billing_period):"]
    source_lines.extend(witness_prelude_lines(["account_number", "billing_period"], include_month_helper=True))
    source_lines.append(f"    return {expr}")
    namespace: dict[str, Any] = {"Exception": Exception}
    exec("\n".join(source_lines), namespace)
    block = namespace["_block"]
    assert block(_Page(), "100248", "June 2026") == "a[href='/statements/100248_2026-06.pdf']"
    with pytest.raises(Exception):
        block(_Page(), "100245'] , x[href='", "May 2026")
    with pytest.raises(Exception):
        block(_Page(), "100245", "Notamonth 2026")


def test_build_input_templated_locator_recompute_and_tamper() -> None:
    holes = input_correspondences_for_interaction({"tool_name": "click", "selector": _STATEMENT_SELECTOR}, _DECLARED)
    expr = build_input_templated_locator(
        surface="selector", selector=_STATEMENT_SELECTOR, role="", name="", holes=holes
    )
    recomputed = build_input_templated_locator(
        surface="selector", selector=_STATEMENT_SELECTOR, role="", name="", holes=holes
    )
    assert expr == recomputed
    reordered = build_input_templated_locator(
        surface="selector", selector=_STATEMENT_SELECTOR, role="", name="", holes=list(reversed(holes))
    )
    assert reordered != expr


def test_synthesize_unwitnessed_selector_byte_identical() -> None:
    plain = {
        "tool_name": "click",
        "selector": _STATEMENT_SELECTOR,
        "accessible_name": "Download",
        "source_url": "https://example.com/s",
    }
    baseline = synthesize_code_block([dict(plain)])
    assert baseline is not None
    assert INPUT_TEMPLATED_PROVENANCE_SOURCE not in baseline.code
    assert all(p.get("source") != LOCATOR_WITNESS_PARAM_SOURCE for p in baseline.parameters)
    assert "_scout_month_to_iso" not in baseline.code


def test_synthesize_input_purity() -> None:
    trajectory = [_witnessed_click()]
    snapshot = json.loads(json.dumps(trajectory))
    synthesize_code_block(list(trajectory), strict_selectors=False)
    synthesize_code_block(list(trajectory), strict_selectors=True)
    assert trajectory == snapshot


def _credential_fill(**overrides: Any) -> dict[str, Any]:
    fields = {
        "selector": "#username",
        "source_url": "https://example.com/login",
        "credential_id": "cred_1",
        "credential_name": "portal",
        "credential_field": "username",
        "typed_length": 8,
    }
    fields.update(overrides)
    return _interaction(CREDENTIAL_FILL_TOOL_NAME, **fields)


class _FakeLocator:
    def __init__(self, page: _FakePage, selector: str) -> None:
        self._page = page
        self._selector = selector

    async def count(self) -> int:
        return self._page.counts.get(self._selector, 0)

    async def wait_for(self, *, state: str, timeout: float | None = None) -> None:
        if self._page.counts.get(self._selector, 0) < 1:
            raise TimeoutError(f"{self._selector} not visible")

    async def fill(self, value: object) -> None:
        if self._page.counts.get(self._selector, 0) != 1:
            raise AssertionError(
                f"strict-mode fill on {self._selector} with count {self._page.counts.get(self._selector, 0)}"
            )
        self._page.filled.append(self._selector)

    async def click(self) -> None:
        self._page.clicked.append(self._selector)

    async def press(self, key: str) -> None:
        self._page.pressed.append((self._selector, key))


class _FakePage:
    def __init__(self, counts: dict[str, int]) -> None:
        self.counts = counts
        self.filled: list[str] = []
        self.clicked: list[str] = []
        self.pressed: list[tuple[str, str]] = []
        self.goto_calls: list[str] = []

    def locator(self, selector: str) -> _FakeLocator:
        return _FakeLocator(self, selector)

    async def goto(self, url: str, *, wait_until: str | None = None) -> None:
        self.goto_calls.append(url)

    async def wait_for_load_state(self, state: str) -> None:
        return None


def _run_synthesized_block(code: str, page: _FakePage, portal: object) -> None:
    namespace: dict[str, Any] = {}
    exec("async def _block(page, portal):\n" + code, namespace)
    asyncio.run(namespace["_block"](page, portal))


class TestLoginOnlyPresenceGuardSynthesis:
    def _login_only_trajectory(self, *, submit: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            _credential_fill(selector="#username", credential_field="username"),
            _credential_fill(selector="#password", credential_field="password"),
            submit,
        ]

    def test_click_submit_wraps_whole_prefix_in_count_guard(self) -> None:
        traj = self._login_only_trajectory(
            submit=_interaction("click", selector="#login-btn", source_url="https://example.com/login")
        )
        result = synthesize_code_block(traj, strict_selectors=True)
        assert result is not None
        assert f"if await {_ENTRY_TARGET_VAR}.count() == 1:" in result.code
        guard_line = next(i for i, ln in enumerate(result.code.splitlines()) if ".count() == 1:" in ln)
        body = result.code.splitlines()[guard_line + 1 : guard_line + 5]
        assert any(".fill(portal.username)" in ln for ln in body)
        assert any(".fill(portal.password)" in ln for ln in body)
        assert any('page.locator("#login-btn").click()' in ln for ln in body)
        assert all(ln.startswith(_INDENT * 2) for ln in body)

    def test_enter_submit_is_recognized_and_guarded(self) -> None:
        traj = self._login_only_trajectory(
            submit=_interaction("press_key", selector="#password", key="Enter", source_url="https://example.com/login")
        )
        result = synthesize_code_block(traj, strict_selectors=True)
        assert result is not None
        assert f"if await {_ENTRY_TARGET_VAR}.count() == 1:" in result.code
        assert '        await page.locator("#password").press("Enter")' in result.code

    def test_present_state_runs_full_login(self) -> None:
        traj = self._login_only_trajectory(
            submit=_interaction("click", selector="#login-btn", source_url="https://example.com/login")
        )
        result = synthesize_code_block(traj, strict_selectors=True)
        assert result is not None
        page = _FakePage({"#username": 1, "#password": 1, "#login-btn": 1})
        _run_synthesized_block(result.code, page, SimpleNamespace(username="u", password="p"))
        assert page.filled == ["#username", "#password"]
        assert page.clicked == ["#login-btn"]

    def test_absent_state_skips_login_without_timeout(self) -> None:
        traj = self._login_only_trajectory(
            submit=_interaction("click", selector="#login-btn", source_url="https://example.com/login")
        )
        result = synthesize_code_block(traj, strict_selectors=True)
        assert result is not None
        page = _FakePage({})
        _run_synthesized_block(result.code, page, SimpleNamespace(username="u", password="p"))
        assert page.filled == []
        assert page.clicked == []
        assert page.goto_calls == ["https://example.com/login"]

    def test_multiple_match_state_does_not_strict_mode_fail(self) -> None:
        traj = self._login_only_trajectory(
            submit=_interaction("click", selector="#login-btn", source_url="https://example.com/login")
        )
        result = synthesize_code_block(traj, strict_selectors=True)
        assert result is not None
        page = _FakePage({"#username": 2, "#password": 2, "#login-btn": 2})
        _run_synthesized_block(result.code, page, SimpleNamespace(username="u", password="p"))
        assert page.filled == []
        assert page.clicked == []


class TestSharedSubmitPredicate:
    def test_click_and_enter_are_submits_but_tab_is_not(self) -> None:
        assert _is_submit_interaction({"tool_name": "click"}) is True
        assert _is_submit_interaction({"tool_name": "press_key", "key": "Enter"}) is True
        assert _is_submit_interaction({"tool_name": "press_key", "key": "Tab"}) is False
        assert _is_submit_interaction({"tool_name": "type_text"}) is False

    def test_tab_only_post_fill_reports_missing_submit(self) -> None:
        trajectory = [
            _credential_fill(selector="#username", credential_field="username"),
            _interaction("press_key", selector="#username", key="Tab", source_url="https://example.com/login"),
        ]
        gap = credential_scout_gap(trajectory, [(frozenset({"cred_1"}), frozenset({"username"}))], requires_submit=True)
        assert gap.missing_submit is True

    def test_enter_post_fill_satisfies_submit(self) -> None:
        trajectory = [
            _credential_fill(selector="#username", credential_field="username"),
            _interaction("press_key", selector="#username", key="Enter", source_url="https://example.com/login"),
        ]
        gap = credential_scout_gap(trajectory, [(frozenset({"cred_1"}), frozenset({"username"}))], requires_submit=True)
        assert gap.missing_submit is False


_LOGIN_HOST = "https://authenticationtest.com/login"
_INLINE_SECRET_SENTINEL = "Hunter2Portal!"


def test_credential_fill_trajectory_binds_param_access_and_omits_literals() -> None:
    trajectory = [
        _credential_fill(
            selector="#username", credential_id="cred_x", credential_field="username", source_url=_LOGIN_HOST
        ),
        _credential_fill(
            selector="#password", credential_id="cred_x", credential_field="password", source_url=_LOGIN_HOST
        ),
        _interaction("click", selector="button[type=submit]", source_url=_LOGIN_HOST),
    ]

    result = synthesize_code_block(trajectory, strict_selectors=True)

    assert result is not None
    credential_param = next(param for param in result.parameters if param.get("credential_id") == "cred_x")
    credential_key = credential_param["key"]
    assert f"{credential_key}.username" in result.code
    assert f"{credential_key}.password" in result.code
    assert _INLINE_SECRET_SENTINEL not in result.code


def test_type_text_secret_bypass_is_not_carried_into_synthesized_block() -> None:
    trajectory = [
        _credential_fill(
            selector="#username", credential_id="cred_x", credential_field="username", source_url=_LOGIN_HOST
        ),
        _interaction(
            "type_text",
            selector="#password",
            source_url=_LOGIN_HOST,
            typed_value="",
            raw_typed_value=_INLINE_SECRET_SENTINEL,
            typed_length=len(_INLINE_SECRET_SENTINEL),
            role="textbox",
        ),
    ]

    result = synthesize_code_block(trajectory, strict_selectors=True)

    assert result is not None
    assert _INLINE_SECRET_SENTINEL not in result.code
    credential_param = next(param for param in result.parameters if param.get("credential_id") == "cred_x")
    assert f"{credential_param['key']}.username" in result.code
