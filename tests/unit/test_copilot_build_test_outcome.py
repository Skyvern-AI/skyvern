from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.copilot.agent import _build_dynamic_system_prompt, _build_user_context, _prior_run_debug_text
from skyvern.forge.sdk.copilot.build_test_outcome import (
    BuildTestEvidencePacket,
    BuildTestPacketDownload,
    BuildTestPacketLocatorObservation,
    BuildTestPacketRegisteredOutput,
    BuildTestPacketRequestedOutput,
    BuildTestPacketUnfinishedItem,
    RecordedBuildTestOutcome,
    authored_block_signatures_from_workflow,
    authored_structure_signature_from_workflow,
    observed_value_extraction_scaffold_lines,
    record_build_test_outcome,
    recorded_outcome_from_author_time_reject,
    recorded_outcome_from_authoring_repair_context,
    recorded_outcome_from_run_blocks_result,
    recorded_outcome_from_scout_act_observe_hollow,
    unresolved_runtime_block_failure,
)
from skyvern.forge.sdk.copilot.completion_verification import CompletionVerificationResult, CriterionVerdict
from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy, CopilotConfig
from skyvern.forge.sdk.copilot.context import CodeAuthoringRepairContext, CopilotContext
from skyvern.forge.sdk.copilot.failure_tracking import selector_identity_from_failure
from skyvern.forge.sdk.copilot.output_utils import project_build_test_packet_for_llm
from skyvern.forge.sdk.copilot.run_outcome import RecordedRunOutcome
from skyvern.forge.sdk.copilot.runtime_authoring_repair import inject_runtime_authoring_repair_context
from skyvern.forge.sdk.copilot.tools import run_execution as run_execution_module
from skyvern.forge.sdk.copilot.tools.run_execution import (
    _authored_literal_locator_selectors,
    _failed_block_code,
    _failing_code_line,
    _first_failed_result,
    _record_run_blocks_result,
    build_test_evidence_packet,
)
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter
from tests.unit.copilot_test_helpers import (
    failed_second_factor_run,
    make_stub_html_artifact,
    passing_run,
    straight_line_login_yaml,
    two_page_login_yaml,
)


def test_structural_key_changes_when_page_or_result_structure_changes() -> None:
    first = RecordedBuildTestOutcome(
        phase="persisted_block_run",
        attempted_tool="update_and_run_blocks",
        verdict="repairable_failure",
        reason_code="runtime_block_failure",
        workflow_run_id="wr_first",
        block_labels=["search_records"],
        structural_failure_identity="runtime:timeout_waiting_for_selector:failed",
        page_evidence_refs=["origin_present", "results:empty"],
        observed_evidence_summary="No matching records.",
    )
    second = first.model_copy(
        update={
            "workflow_run_id": "wr_second",
            "page_evidence_refs": ["origin_present", "results:table_rows"],
            "observed_evidence_summary": "A table with one result row appeared.",
        }
    )

    assert first.structural_key is not None
    assert second.structural_key is not None
    assert first.structural_key != second.structural_key


def test_run_blocks_outcome_records_requested_labels_and_shape_hashes() -> None:
    outcome = recorded_outcome_from_run_blocks_result(
        {
            "ok": False,
            "data": {
                "workflow_run_id": "wr_failed",
                "requested_block_labels": ["open", "search", "extract"],
                "blocks": [
                    {"label": "search", "status": "failed", "failure_type": "runtime_error"},
                ],
                "overall_status": "failed",
                "failure_type": "runtime_error",
            },
        },
        block_shape_hashes={"open": "h1", "search": "h2", "extract": "h3"},
    )

    assert outcome is not None
    assert outcome.requested_block_labels == ["open", "search", "extract"]
    assert outcome.block_shape_hashes == {"open": "h1", "search": "h2", "extract": "h3"}


def _failed_run_result_with_categories(categories: list[dict]) -> dict:
    return {
        "ok": False,
        "data": {
            "workflow_run_id": "wr_failed",
            "overall_status": "failed",
            "failure_categories": categories,
            "blocks": [
                {"label": "search", "status": "failed", "failure_reason": "Timeout waiting for #results"},
            ],
        },
    }


def test_structural_identity_ignores_keyword_only_anti_bot_categories() -> None:
    element_only = recorded_outcome_from_run_blocks_result(
        _failed_run_result_with_categories([{"category": "ELEMENT_NOT_FOUND", "confidence_float": 0.8}])
    )
    with_keyword_stamp = recorded_outcome_from_run_blocks_result(
        _failed_run_result_with_categories(
            [
                {"category": "ELEMENT_NOT_FOUND", "confidence_float": 0.8},
                {"category": "ANTI_BOT_DETECTION", "confidence_float": 0.7, "evidence_source": "keyword_only"},
            ]
        )
    )
    with_carrier = recorded_outcome_from_run_blocks_result(
        _failed_run_result_with_categories(
            [
                {"category": "ELEMENT_NOT_FOUND", "confidence_float": 0.8},
                {"category": "ANTI_BOT_DETECTION", "confidence_float": 0.9, "evidence_source": "challenge_state"},
            ]
        )
    )

    assert element_only is not None and with_keyword_stamp is not None and with_carrier is not None
    assert with_keyword_stamp.structural_failure_identity == element_only.structural_failure_identity
    assert with_carrier.structural_failure_identity != element_only.structural_failure_identity


def test_structural_key_ignores_display_prose_and_workflow_run_id() -> None:
    first = RecordedBuildTestOutcome(
        phase="persisted_block_run",
        attempted_tool="update_and_run_blocks",
        verdict="repairable_failure",
        reason_code="runtime_block_failure",
        workflow_run_id="wr_first",
        block_labels=["search_records"],
        structural_failure_identity="runtime:timeout_waiting_for_selector:failed",
        page_evidence_refs=["origin_present", "form:search"],
        observed_evidence_summary="Timeout waiting for #results.",
        display_text="The page did not show results.",
    )
    second = first.model_copy(
        update={
            "workflow_run_id": "wr_second",
            "observed_evidence_summary": "Different explanation with the same structural observation.",
            "display_text": "Another user-facing sentence.",
        }
    )

    assert first.structural_key == second.structural_key


def test_structural_key_ignores_authored_signature_but_retains_it() -> None:
    first = RecordedBuildTestOutcome(
        phase="persisted_block_run",
        attempted_tool="update_and_run_blocks",
        verdict="repairable_failure",
        reason_code="no_meaningful_output",
        structural_failure_identity="completion:typed-outcome",
        page_evidence_refs=["origin:https://example.com", "result:#results rows=0"],
        authored_structure_signature="authored:first",
    )
    second = first.model_copy(update={"authored_structure_signature": "authored:second"})

    assert first.structural_key is not None
    assert first.structural_key == second.structural_key
    assert first.authored_structure_signature == "authored:first"
    assert second.authored_structure_signature == "authored:second"


def test_scout_act_observe_hollow_outcome_is_structural_and_privacy_bounded() -> None:
    outcome = recorded_outcome_from_scout_act_observe_hollow(
        interaction_tool="click",
        selector="#search",
        current_url="https://example.com/customers/acme-inc/results?token=secret",
        source_url="https://example.com/accounts/claim-123/search?name=customer",
        page_evidence={
            "page_title": "Private Account Search",
            "forms": [],
            "navigation_targets": [],
            "result_containers": [],
            "clickable_controls": [],
            "visible_text": "Customer name should not persist",
            "body": "<main></main>",
            "schema_empty_page": True,
        },
        recapture_attempted=True,
        recapture_result="timeout",
    )

    key_payload = outcome.structural_key_payload

    assert outcome.reason_code == "scout_act_observe_hollow_after_interaction"
    assert outcome.structural_key is not None
    assert outcome.is_authoritative is True
    assert "recapture_attempted:true" in outcome.page_evidence_refs
    assert "recapture_result:timeout" in outcome.page_evidence_refs
    for sensitive in ("token=secret", "name=customer", "acme-inc", "claim-123", "Customer name", "Private Account"):
        assert sensitive not in str(key_payload)


def test_hollow_outcome_carries_observed_value_excerpt_off_the_structural_key() -> None:
    def _outcome(visible_text: str) -> RecordedBuildTestOutcome:
        return recorded_outcome_from_scout_act_observe_hollow(
            interaction_tool="click",
            selector="#submit",
            current_url="https://example.com/confirmation",
            source_url="https://example.com/form",
            page_evidence={
                "page_title": "Confirmation",
                "forms": [],
                "visible_text_excerpt": visible_text,
            },
            recapture_attempted=True,
            recapture_result="hollow",
        )

    confirmation = _outcome("Request WTR-1842-DEMO for account 100245 confirmed")
    other = _outcome("A completely different confirmation body")

    assert "WTR-1842-DEMO" in confirmation.observed_page_value_excerpt
    assert "100245" in confirmation.observed_page_value_excerpt
    assert confirmation.structural_key == other.structural_key
    assert "WTR-1842-DEMO" not in str(confirmation.structural_key_payload)


def test_hollow_outcome_reason_code_unchanged_with_value_carrying_relation() -> None:
    outcome = recorded_outcome_from_scout_act_observe_hollow(
        interaction_tool="click",
        selector="#view-statement",
        current_url="https://portal.example.com/statement",
        source_url="https://portal.example.com/statement",
        page_evidence={
            "page_title": "Statement",
            "forms": [],
            "key_value_relations": [
                {
                    "key_text": "March 2026 statement",
                    "value_text": "Amount due: $3,927.75",
                    "container_selector": "#result",
                    "value_child_index": 1,
                    "direct_child_count": 3,
                    "visible": True,
                    "value_visible": True,
                }
            ],
            "key_value_relations_truncated": False,
        },
        recapture_attempted=True,
        recapture_result="hollow",
    )

    assert outcome.reason_code == "scout_act_observe_hollow_after_interaction"
    assert outcome.is_authoritative is True
    assert "$3,927.75" not in str(outcome.structural_key_payload)


def test_hollow_outcome_value_excerpt_falls_back_to_legacy_text_keys() -> None:
    from_visible_text = recorded_outcome_from_scout_act_observe_hollow(
        interaction_tool="click",
        selector="#submit",
        current_url="https://example.com/confirmation",
        source_url=None,
        page_evidence={"visible_text": "Legacy visible text body"},
        recapture_attempted=False,
        recapture_result="not_attempted_no_budget",
    )
    from_body_text = recorded_outcome_from_scout_act_observe_hollow(
        interaction_tool="click",
        selector="#submit",
        current_url="https://example.com/confirmation",
        source_url=None,
        page_evidence={"bodyText": "Legacy body text body"},
        recapture_attempted=False,
        recapture_result="not_attempted_no_budget",
    )

    assert from_visible_text.observed_page_value_excerpt == "Legacy visible text body"
    assert from_body_text.observed_page_value_excerpt == "Legacy body text body"


def test_hollow_outcome_value_excerpt_is_bounded_and_key_independent() -> None:
    long_text = "X" * 5000
    outcome = recorded_outcome_from_scout_act_observe_hollow(
        interaction_tool="click",
        selector="#submit",
        current_url="https://example.com/confirmation",
        source_url=None,
        page_evidence={"visible_text_excerpt": long_text},
        recapture_attempted=True,
        recapture_result="hollow",
    )
    baseline = recorded_outcome_from_scout_act_observe_hollow(
        interaction_tool="click",
        selector="#submit",
        current_url="https://example.com/confirmation",
        source_url=None,
        page_evidence={"visible_text_excerpt": ""},
        recapture_attempted=True,
        recapture_result="hollow",
    )

    assert 0 < len(outcome.observed_page_value_excerpt) <= 700
    assert baseline.observed_page_value_excerpt == ""
    assert outcome.structural_key == baseline.structural_key


def test_author_time_reject_carries_value_excerpt_off_the_convergence_key() -> None:
    carried = recorded_outcome_from_author_time_reject(
        reason_code="metadata_reject",
        structural_payload={"version": "metadata_reject_output_contract:v1", "signature": "abc"},
        observed_page_value_excerpt="  Request WTR-1842-DEMO for account 100245  " + "detail " * 400,
        missing_requested_output_facts=[{"output_path": "output.confirmation_number"}],
    )
    baseline = recorded_outcome_from_author_time_reject(
        reason_code="metadata_reject",
        structural_payload={"version": "metadata_reject_output_contract:v1", "signature": "abc"},
        missing_requested_output_facts=[{"output_path": "output.confirmation_number"}],
    )

    assert "WTR-1842-DEMO" in carried.observed_page_value_excerpt
    assert 0 < len(carried.observed_page_value_excerpt) <= 700
    assert "WTR-1842-DEMO" not in str(carried.structural_key_payload)
    assert carried.structural_key == baseline.structural_key


def test_observed_value_extraction_scaffold_binds_output_paths() -> None:
    scaffold = observed_value_extraction_scaffold_lines(
        "Request WTR-1842-DEMO for account 100245",
        ["output.confirmation_number", "output.account_number", "output.confirmation_number"],
    )

    assert scaffold[0].startswith("OBSERVED PAGE VALUES CONTRACT")
    assert "observed_values: Request WTR-1842-DEMO for account 100245" in scaffold
    assert "bind_output_paths:" in scaffold
    assert "- output.confirmation_number: <observed value>" in scaffold
    assert "- output.account_number: <observed value>" in scaffold
    assert sum(1 for line in scaffold if line.startswith("- output.confirmation_number")) == 1


def test_observed_value_extraction_scaffold_without_paths_surfaces_values_only() -> None:
    assert observed_value_extraction_scaffold_lines("Confirmed WTR-1842-DEMO", []) == [
        "observed_page_values: Confirmed WTR-1842-DEMO"
    ]
    assert observed_value_extraction_scaffold_lines("   ", ["output.x"]) == []


def test_prose_or_label_only_typed_outcome_is_not_authoritative() -> None:
    outcome = RecordedBuildTestOutcome(
        phase="author_time_reject",
        attempted_tool="update_workflow",
        attempted_block_label="search_records",
        verdict="authoring_rejected",
        reason_code="code_safety_reject",
        block_labels=["search_records"],
        observed_evidence_summary="This sounds like the same failure.",
        display_text="Use the prior failure reason.",
    )

    assert outcome.structural_key is None
    assert outcome.is_authoritative is False


def test_record_none_clears_stale_latest_outcome() -> None:
    ctx = SimpleNamespace(
        latest_recorded_build_test_outcome=None,
        recorded_build_test_outcome_history=[],
        recorded_persisted_block_run_workflow_run_id=None,
    )
    record_build_test_outcome(
        ctx,
        RecordedBuildTestOutcome(
            phase="persisted_block_run",
            attempted_tool="update_and_run_blocks",
            verdict="repairable_failure",
            reason_code="runtime_block_failure",
            structural_failure_identity="typed-runtime-identity",
        ),
    )
    assert ctx.latest_recorded_build_test_outcome is not None

    record_build_test_outcome(ctx, None)

    assert ctx.latest_recorded_build_test_outcome is None


def test_record_authoritative_persisted_run_latches_run_backed_evidence() -> None:
    ctx = SimpleNamespace(
        latest_recorded_build_test_outcome=None,
        recorded_build_test_outcome_history=[],
        recorded_persisted_block_run_workflow_run_id=None,
    )
    record_build_test_outcome(
        ctx,
        RecordedBuildTestOutcome(
            phase="persisted_block_run",
            attempted_tool="update_and_run_blocks",
            verdict="repairable_failure",
            reason_code="runtime_block_failure",
            workflow_run_id="wr_recorded",
            structural_failure_identity="runtime:failed",
        ),
    )
    record_build_test_outcome(
        ctx,
        RecordedBuildTestOutcome(
            phase="author_time_reject",
            attempted_tool="update_workflow",
            verdict="authoring_rejected",
            reason_code="metadata_reject",
            structural_failure_identity="metadata:missing",
        ),
    )

    assert ctx.recorded_persisted_block_run_workflow_run_id == "wr_recorded"
    assert ctx.recorded_persisted_block_run_workflow_run_id == "wr_recorded"


def test_non_authoritative_persisted_run_does_not_latch_run_backed_evidence() -> None:
    ctx = SimpleNamespace(
        latest_recorded_build_test_outcome=None,
        recorded_build_test_outcome_history=[],
        recorded_persisted_block_run_workflow_run_id=None,
    )
    record_build_test_outcome(
        ctx,
        RecordedBuildTestOutcome(
            phase="persisted_block_run",
            attempted_tool="update_and_run_blocks",
            verdict="repairable_failure",
            reason_code="runtime_block_failure",
            workflow_run_id="wr_hollow",
        ),
    )

    assert ctx.recorded_persisted_block_run_workflow_run_id is None
    assert ctx.recorded_persisted_block_run_workflow_run_id is None


def test_authored_structure_signature_is_stable_and_excludes_raw_code_or_prose() -> None:
    workflow_yaml = """
    title: Registry lookup
    workflow_definition:
      parameters:
      - parameter_type: workflow
        workflow_parameter_type: string
        key: provider_query
      blocks:
      - block_type: code
        label: search_registry
        parameter_keys:
        - provider_query
        code: |
          await page.goto("https://example.com/search")
          return {"records": [{"npi": "123"}]}
    """
    metadata = [
        {
            "block_label": "search_registry",
            "declared_goal": "Find the exact provider row from the page prose.",
            "claimed_outcomes": [
                {
                    "id": "claim:provider",
                    "text": "The provider was found in the directory.",
                    "goal_value_paths": ["records[].npi"],
                    "extraction_schema": {
                        "type": "object",
                        "properties": {"records": {"type": "array", "items": {"type": "object"}}},
                    },
                }
            ],
        }
    ]
    prose_changed_metadata = [
        {
            **metadata[0],
            "declared_goal": "Different page prose for the same structure.",
            "claimed_outcomes": [{**metadata[0]["claimed_outcomes"][0], "text": "Different prose."}],
        }
    ]

    signature = authored_structure_signature_from_workflow(workflow_yaml, metadata)
    same_structure = authored_structure_signature_from_workflow(workflow_yaml, prose_changed_metadata)
    dumped = RecordedBuildTestOutcome(
        phase="persisted_block_run",
        attempted_tool="update_and_run_blocks",
        verdict="repairable_failure",
        reason_code="no_meaningful_output",
        structural_failure_identity="completion:typed",
        authored_structure_signature=signature,
    ).model_dump(mode="json")

    assert signature is not None
    assert signature == same_structure
    assert "page.goto" not in str(dumped)
    assert "Find the exact provider" not in str(dumped)


def test_authored_block_signatures_ignore_cosmetic_block_fields() -> None:
    base = """
    title: Registry lookup
    workflow_definition:
      blocks:
      - block_type: code
        label: search_registry
        parameter_keys:
        - provider_query
        code: |
          return {"records": [{"npi": "123"}]}
    """
    described = base.replace(
        "        label: search_registry\n",
        "        label: search_registry\n        description: look up the provider by name\n",
    )
    continue_on_failure = base.replace(
        "        label: search_registry\n",
        "        label: search_registry\n        continue_on_failure: true\n",
    )
    renamed = base.replace("search_registry", "lookup_registry")

    baseline = authored_block_signatures_from_workflow(base, None)
    assert authored_block_signatures_from_workflow(described, None) == baseline
    assert authored_block_signatures_from_workflow(continue_on_failure, None) == baseline

    renamed_signatures = authored_block_signatures_from_workflow(renamed, None)
    assert set(renamed_signatures) == {"lookup_registry"}
    assert renamed_signatures["lookup_registry"] == baseline["search_registry"]


def test_authored_structure_signature_changes_on_code_parameter_or_output_structure() -> None:
    base = """
    title: Registry lookup
    workflow_definition:
      blocks:
      - block_type: code
        label: search_registry
        parameter_keys:
        - provider_query
        code: |
          return {"records": [{"npi": "123"}]}
    """
    changed_code = base.replace('"123"', '"456"')
    changed_parameter = base.replace("provider_query", "provider_name")
    metadata = [{"block_label": "search_registry", "claimed_outcomes": [{"goal_value_paths": ["records[].npi"]}]}]
    changed_metadata = [
        {"block_label": "search_registry", "claimed_outcomes": [{"goal_value_paths": ["records[].license"]}]}
    ]

    signature = authored_structure_signature_from_workflow(base, metadata)

    assert signature is not None
    assert authored_structure_signature_from_workflow(changed_code, metadata) != signature
    assert authored_structure_signature_from_workflow(changed_parameter, metadata) != signature
    assert authored_structure_signature_from_workflow(base, changed_metadata) != signature


def test_no_meaningful_output_keeps_authoritative_unsatisfied_criteria_identity() -> None:
    result = {
        "ok": True,
        "data": {
            "workflow_run_id": "wr_partial",
            "overall_status": "completed",
            "blocks": [
                {
                    "label": "lookup_provider_and_extract_credentials",
                    "status": "completed",
                    "extracted_data": {"npi": "", "evidence_text": "address and statuses appear in page text"},
                }
            ],
        },
    }
    verification = CompletionVerificationResult(
        status="evaluated",
        criterion_ids=["npi", "locations", "statuses", "overall"],
        verdicts=[
            CriterionVerdict(criterion_id="npi", state="unsatisfied", reason_code="no_evidence", output_path="npi"),
            CriterionVerdict(
                criterion_id="locations",
                state="unsatisfied",
                reason_code="no_evidence",
                output_path="locations",
            ),
            CriterionVerdict(
                criterion_id="statuses",
                state="unsatisfied",
                reason_code="no_evidence",
                output_path="credentialing_statuses",
            ),
            CriterionVerdict(
                criterion_id="overall",
                state="unsatisfied",
                reason_code="no_evidence",
                output_path="overall_credentialing_status",
            ),
        ],
    )

    outcome = recorded_outcome_from_run_blocks_result(
        result,
        recorded_run_outcome=RecordedRunOutcome(
            verdict="not_demonstrated",
            reason_code="no_meaningful_output",
            workflow_run_id="wr_partial",
        ),
        completion_verification=verification,
        authored_structure_signature="authored:partial-location-only",
    )

    assert outcome is not None
    assert outcome.phase == "persisted_block_run"
    assert outcome.reason_code == "no_meaningful_output"
    assert outcome.workflow_run_id == "wr_partial"
    assert outcome.is_authoritative is True
    assert outcome.structural_key is not None
    assert outcome.authored_structure_signature == "authored:partial-location-only"
    assert outcome.key_provenance["structural_failure_identity"] == "CompletionVerificationResult verdict structure"
    assert outcome.missing_requested_output_facts == [
        {
            "criterion_id": "statuses",
            "output_path": "credentialing_statuses",
            "output_root": "credentialing_statuses",
            "reason_code": "no_evidence",
            "value_status": "no_typed_value",
            "partial_output_block_labels": ["lookup_provider_and_extract_credentials"],
        },
        {
            "criterion_id": "locations",
            "output_path": "locations",
            "output_root": "locations",
            "reason_code": "no_evidence",
            "value_status": "no_typed_value",
            "partial_output_block_labels": ["lookup_provider_and_extract_credentials"],
        },
        {
            "criterion_id": "npi",
            "output_path": "npi",
            "output_root": "npi",
            "reason_code": "no_evidence",
            "value_status": "empty_typed_value",
        },
        {
            "criterion_id": "overall",
            "output_path": "overall_credentialing_status",
            "output_root": "overall_credentialing_status",
            "reason_code": "no_evidence",
            "value_status": "no_typed_value",
            "partial_output_block_labels": ["lookup_provider_and_extract_credentials"],
        },
    ]
    payload_text = str(outcome.structural_key_payload)
    assert "evidence_text" not in payload_text
    assert "address and statuses" not in payload_text


def test_not_evaluated_recorded_outcome_is_not_authoritative_repair_failure() -> None:
    result = {
        "ok": True,
        "data": {
            "workflow_run_id": "wr_structural",
            "blocks": [
                {
                    "label": "publish_result",
                    "status": "completed",
                    "extracted_data": {"document_name": "Resale Demand Package"},
                }
            ],
        },
    }
    verification = CompletionVerificationResult(
        status="evaluated",
        criterion_ids=["c0"],
        verdicts=[
            CriterionVerdict(
                criterion_id="c0",
                state="unsatisfied",
                reason_code="structurally_abstained",
                output_path="output.document_name",
            )
        ],
    )

    outcome = recorded_outcome_from_run_blocks_result(
        result,
        recorded_run_outcome=RecordedRunOutcome(verdict="not_evaluated", workflow_run_id="wr_structural"),
        completion_verification=verification,
    )

    assert outcome is not None
    assert outcome.verdict == "not_authoritative"
    assert outcome.reason_code == "run_completed_unevaluated"
    assert outcome.is_authoritative is False


def test_completed_run_with_registered_outputs_is_not_classified_as_failed_run() -> None:
    result = {
        "ok": True,
        "data": {
            "workflow_run_id": "wr_completed",
            "overall_status": "completed",
            "blocks": [{"label": "collect_top_entry", "status": "completed"}],
            "registered_output_parameter_values": [
                {"output_parameter_id": "op_1", "value": {"output": {"top_entry": "First listed entry"}}}
            ],
        },
    }

    outcome = recorded_outcome_from_run_blocks_result(
        result,
        recorded_run_outcome=RecordedRunOutcome(verdict="not_evaluated", workflow_run_id="wr_completed"),
    )

    assert outcome is not None
    assert outcome.reason_code != "failed_run"
    assert outcome.reason_code == "run_completed_unevaluated"


def test_failed_run_classification_preserved_for_not_ok_run() -> None:
    result = {
        "ok": False,
        "data": {
            "workflow_run_id": "wr_not_ok",
            "overall_status": "failed",
            "failure_type": "block_failure",
            "blocks": [{"label": "collect_top_entry", "status": "completed"}],
        },
    }

    outcome = recorded_outcome_from_run_blocks_result(result)

    assert outcome is not None
    assert outcome.reason_code == "runtime_block_failure"
    assert outcome.verdict == "repairable_failure"


def test_recorded_failed_run_preserves_runtime_block_identity() -> None:
    for run_ok in (False, True):
        result = {
            "ok": run_ok,
            "data": {
                "workflow_run_id": "wr_not_ok",
                "overall_status": "failed",
                "failure_type": "block_failure",
                "blocks": [
                    {
                        "label": "collect_top_entry",
                        "status": "failed",
                        "failure_reason": (
                            "TimeoutError: Locator.click: Timeout 30000ms exceeded while waiting for "
                            'locator("#cta-stale-regression-probe")'
                        ),
                    }
                ],
            },
        }

        outcome = recorded_outcome_from_run_blocks_result(
            result,
            recorded_run_outcome=RecordedRunOutcome(
                verdict="not_demonstrated",
                reason_code="blocker_reported",
                workflow_run_id="wr_not_ok",
            ),
        )

        assert outcome is not None
        assert outcome.reason_code == "runtime_block_failure"
        assert outcome.verdict == "repairable_failure"
        assert outcome.attempted_block_label == "collect_top_entry"
        assert outcome.attempted_call_ref == "locator:#cta-stale-regression-probe"


def test_failed_run_classification_preserved_for_failed_block() -> None:
    result = {
        "ok": True,
        "data": {
            "workflow_run_id": "wr_failed_block",
            "overall_status": "completed",
            "failure_type": "block_failure",
            "blocks": [{"label": "collect_top_entry", "status": "failed", "failure_reason": "selector not found"}],
        },
    }

    outcome = recorded_outcome_from_run_blocks_result(result)

    assert outcome is not None
    assert outcome.reason_code == "runtime_block_failure"


def test_judge_evaluated_non_satisfaction_keeps_failure_classification() -> None:
    result = {
        "ok": True,
        "data": {
            "workflow_run_id": "wr_judged",
            "overall_status": "completed",
            "blocks": [{"label": "collect_top_entry", "status": "completed"}],
        },
    }

    outcome = recorded_outcome_from_run_blocks_result(
        result,
        recorded_run_outcome=RecordedRunOutcome(
            verdict="not_demonstrated",
            reason_code="no_meaningful_output",
            workflow_run_id="wr_judged",
        ),
    )

    assert outcome is not None
    assert outcome.reason_code == "no_meaningful_output"


def test_no_meaningful_output_does_not_mark_presence_only_abstention_as_missing_output() -> None:
    result = {
        "ok": True,
        "data": {
            "workflow_run_id": "wr_top_post",
            "overall_status": "completed",
            "blocks": [
                {
                    "label": "extract_top_hn_post",
                    "status": "completed",
                    "extracted_data": {"output": {"top_post": "Claude Sonnet 5"}},
                }
            ],
        },
    }
    verification = CompletionVerificationResult(
        status="evaluated",
        criterion_ids=["top_post"],
        verdicts=[
            CriterionVerdict(
                criterion_id="top_post",
                state="unsatisfied",
                reason_code="structurally_abstained",
                output_path="output.top_post",
                grounding_mode="missing",
                evidence_ref="block_outputs:extract_top_hn_post.output.top_post",
            )
        ],
    )

    outcome = recorded_outcome_from_run_blocks_result(
        result,
        recorded_run_outcome=RecordedRunOutcome(
            verdict="not_demonstrated",
            reason_code="no_meaningful_output",
            workflow_run_id="wr_top_post",
        ),
        completion_verification=verification,
    )

    assert outcome is not None
    assert outcome.reason_code == "no_meaningful_output"
    assert outcome.is_authoritative is True
    assert outcome.missing_requested_output_facts == []


def test_no_meaningful_output_keeps_missing_fact_for_absent_requested_output() -> None:
    result = {
        "ok": True,
        "data": {
            "workflow_run_id": "wr_no_top_post",
            "overall_status": "completed",
            "blocks": [
                {
                    "label": "extract_top_hn_post",
                    "status": "completed",
                    "extracted_data": {"output": {}},
                }
            ],
        },
    }
    verification = CompletionVerificationResult(
        status="evaluated",
        criterion_ids=["top_post"],
        verdicts=[
            CriterionVerdict(
                criterion_id="top_post",
                state="unsatisfied",
                reason_code="no_evidence",
                output_path="output.top_post",
                grounding_mode="missing",
            )
        ],
    )

    outcome = recorded_outcome_from_run_blocks_result(
        result,
        recorded_run_outcome=RecordedRunOutcome(
            verdict="not_demonstrated",
            reason_code="no_meaningful_output",
            workflow_run_id="wr_no_top_post",
        ),
        completion_verification=verification,
    )

    assert outcome is not None
    assert outcome.reason_code == "no_meaningful_output"
    assert outcome.missing_requested_output_facts == [
        {
            "criterion_id": "top_post",
            "output_path": "output.top_post",
            "output_root": "output",
            "reason_code": "no_evidence",
            "value_status": "no_typed_value",
            "grounding_mode": "missing",
        }
    ]
    assert outcome.structural_key_payload is not None
    assert "output.top_post" in str(outcome.structural_key_payload)


def test_synthesized_parameter_repair_context_produces_structural_recorded_outcome() -> None:
    context = CodeAuthoringRepairContext(
        block_label="search_records",
        reason_code="synthesized_parameter_binding_ambiguous",
        unresolved_names=["confirmation_number", "row_text"],
        parameter_keys=["confirmation_number"],
        available_parameter_keys=["confirmation_number"],
        binding_candidates=["confirmation_number", "row_text"],
    )

    outcome = recorded_outcome_from_authoring_repair_context(context)

    assert outcome.phase == "author_time_reject"
    assert outcome.reason_code == "synthesized_parameter_binding_ambiguous"
    assert outcome.structural_key is not None
    assert (
        outcome.structural_key
        == recorded_outcome_from_authoring_repair_context(
            context.model_copy(update={"unresolved_names": ["row_text", "confirmation_number"]})
        ).structural_key
    )


def test_authoring_repair_context_missing_output_fields_affect_structural_outcome() -> None:
    context = CodeAuthoringRepairContext(
        block_label="read_resource_table",
        reason_code="runtime_missing_output_dependency",
        missing_output_key="create_resource_output",
        available_output_keys=["search_output"],
        current_block_parameter_keys=["create_resource_output"],
        output_dependency_failure_class="missing_prior_block_output",
    )

    outcome = recorded_outcome_from_authoring_repair_context(context)

    assert outcome.reason_code == "runtime_missing_output_dependency"
    assert (
        outcome.structural_key
        != recorded_outcome_from_authoring_repair_context(
            context.model_copy(update={"missing_output_key": "verify_resource_output"})
        ).structural_key
    )
    assert (
        outcome.structural_key
        != recorded_outcome_from_authoring_repair_context(
            context.model_copy(update={"available_output_keys": ["search_output", "verify_resource_output"]})
        ).structural_key
    )


def test_author_time_reject_structural_payloads_make_distinct_keys() -> None:
    first = recorded_outcome_from_author_time_reject(
        reason_code="schema_incompatibility",
        structural_payload={
            "block_label": "extract_record",
            "incompatible_paths": ["records[].expiration_date"],
            "known_output_paths": ["records[].name"],
        },
    )
    second = recorded_outcome_from_author_time_reject(
        reason_code="schema_incompatibility",
        structural_payload={
            "block_label": "extract_record",
            "incompatible_paths": ["records[].license_number"],
            "known_output_paths": ["records[].name"],
        },
    )

    assert first.structural_key is not None
    assert second.structural_key is not None
    assert first.structural_key != second.structural_key


def test_metadata_reject_preserves_missing_requested_output_facts() -> None:
    outcome = recorded_outcome_from_author_time_reject(
        reason_code="metadata_reject",
        structural_payload={
            "reason_code": "recorded_outcome_missing_output_coverage",
            "missing_output_roots": ["address", "credentialing_status"],
            "block_labels": ["lookup_provider_and_extract_credentials"],
        },
        missing_requested_output_facts=[
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
        ],
    )

    assert outcome.reason_code == "metadata_reject"
    assert outcome.is_authoritative is True
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
    assert outcome.structural_key_payload is not None
    assert "address" in str(outcome.structural_key_payload)


def test_author_time_reject_without_structural_payload_is_not_authoritative() -> None:
    outcome = recorded_outcome_from_author_time_reject(
        reason_code="code_safety_reject",
        observed_evidence_summary="Rewrite the code without unsafe behavior.",
    )

    assert outcome.is_authoritative is False
    assert outcome.structural_key is None


def test_metadata_reject_key_uses_typed_fields_not_wording() -> None:
    first = recorded_outcome_from_author_time_reject(
        reason_code="metadata_reject",
        structural_payload={
            "reason_code": "metadata_reject",
            "offending_labels": ["search_registry"],
            "required_fields": ["claimed_outcomes", "completion_criteria"],
            "missing_fields_by_label": {"search_registry": ["claimed_outcomes"]},
            "violation_categories": ["missing_required_list"],
        },
        observed_evidence_summary="Metadata requires non-empty claimed_outcomes.",
    )
    same_structure = recorded_outcome_from_author_time_reject(
        reason_code="metadata_reject",
        structural_payload={
            "reason_code": "metadata_reject",
            "offending_labels": ["search_registry"],
            "required_fields": ["claimed_outcomes", "completion_criteria"],
            "missing_fields_by_label": {"search_registry": ["claimed_outcomes"]},
            "violation_categories": ["missing_required_list"],
        },
        observed_evidence_summary="Different wording for the same typed metadata failure.",
    )
    changed_label = recorded_outcome_from_author_time_reject(
        reason_code="metadata_reject",
        structural_payload={
            "reason_code": "metadata_reject",
            "offending_labels": ["extract_registry"],
            "required_fields": ["claimed_outcomes", "completion_criteria"],
            "missing_fields_by_label": {"extract_registry": ["claimed_outcomes"]},
            "violation_categories": ["missing_required_list"],
        },
    )
    changed_required_field = recorded_outcome_from_author_time_reject(
        reason_code="metadata_reject",
        structural_payload={
            "reason_code": "metadata_reject",
            "offending_labels": ["search_registry"],
            "required_fields": ["terminal_verifier_expectations"],
            "missing_fields_by_label": {"search_registry": ["terminal_verifier_expectations"]},
            "violation_categories": ["missing_required_list"],
        },
    )
    changed_missing_field = recorded_outcome_from_author_time_reject(
        reason_code="metadata_reject",
        structural_payload={
            "reason_code": "metadata_reject",
            "offending_labels": ["search_registry"],
            "required_fields": ["claimed_outcomes", "completion_criteria"],
            "missing_fields_by_label": {"search_registry": ["completion_criteria"]},
            "violation_categories": ["missing_required_list"],
        },
    )

    assert first.structural_key == same_structure.structural_key
    assert changed_label.structural_key != first.structural_key
    assert changed_required_field.structural_key != first.structural_key
    assert changed_missing_field.structural_key != first.structural_key


def test_output_policy_reject_key_uses_stable_trace_payload() -> None:
    payload = {
        "surface": "tool_body",
        "tool_name": "update_workflow",
        "allowed": False,
        "output_kind": "workflow_update_proposal",
        "reason_codes": ["raw_secret_leak"],
    }
    first = recorded_outcome_from_author_time_reject(
        reason_code="output_policy_reject",
        structural_payload=payload,
        observed_evidence_summary="Output policy blocked this Copilot output before persistence.",
    )
    same_structure = recorded_outcome_from_author_time_reject(
        reason_code="output_policy_reject",
        structural_payload=dict(payload),
        observed_evidence_summary="Different wording for the same output-policy reject.",
    )
    changed_reason = recorded_outcome_from_author_time_reject(
        reason_code="output_policy_reject",
        structural_payload={**payload, "reason_codes": ["unapproved_credential_reference"]},
    )
    changed_surface = recorded_outcome_from_author_time_reject(
        reason_code="output_policy_reject",
        structural_payload={**payload, "surface": "final_response"},
    )

    assert first.reason_code == "output_policy_reject"
    assert first.is_authoritative is True
    assert first.structural_key == same_structure.structural_key
    assert changed_reason.structural_key != first.structural_key
    assert changed_surface.structural_key != first.structural_key


def test_runtime_block_failure_outcome_includes_bounded_page_state_and_run_id() -> None:
    result = {
        "ok": False,
        "error": "Timeout waiting for results.",
        "data": {
            "workflow_run_id": "wr_failed",
            "overall_status": "failed",
            "blocks": [
                {
                    "label": "search_records",
                    "status": "failed",
                    "failure_reason": "Timeout waiting for results.",
                }
            ],
        },
    }
    page_evidence = {
        "workflow_run_id": "wr_failed",
        "observed_after_workflow_run": True,
        "source_tool": "inspect_page_for_composition",
        "current_url": "https://example.test/search?secret=redacted",
        "page_title": "Search",
        "forms": [{"fields": [{"label": "Search", "selector": "#search"}]}],
        "result_containers": [{"selector": "#results", "text_excerpt": "No matching records"}],
    }

    outcome = recorded_outcome_from_run_blocks_result(result, page_evidence=page_evidence)

    assert outcome.workflow_run_id == "wr_failed"
    assert outcome.phase == "persisted_block_run"
    assert outcome.reason_code == "runtime_block_failure"
    assert outcome.structural_key is not None
    assert "form:Search #search" in outcome.page_evidence_refs
    assert "result:#results rows=unknown" in outcome.page_evidence_refs


@pytest.mark.asyncio
async def test_failed_run_complete_fact_packet_reaches_ordinary_repair_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _locator_packet_ctx()
    now = datetime(2026, 8, 28, tzinfo=UTC)
    output_parameter = OutputParameter(
        output_parameter_id="out_records",
        workflow_id="wf_run_snapshot",
        key="records",
        description="Collected records",
        created_at=now,
        modified_at=now,
    )
    run_workflow = SimpleNamespace(
        organization_id=ctx.organization_id,
        workflow_definition=SimpleNamespace(
            parameters=[output_parameter],
            blocks=[SimpleNamespace(label="collect_records", block_type="CODE", output_parameter=output_parameter)],
        ),
    )
    run = SimpleNamespace(
        workflow_permanent_id=ctx.workflow_permanent_id,
        browser_session_id="pbs_failed_complete_packet",
        status="failed",
        failure_reason="Code block failed.",
    )
    block = SimpleNamespace(
        label="collect_records",
        block_type=SimpleNamespace(name="CODE"),
        status="failed",
        failure_reason="NameError at generated line 7",
        error_codes=["user_code_error"],
        output=None,
        final_url="https://example.test/results",
    )
    artifact = make_stub_html_artifact("art_failed_terminal", ArtifactType.HTML_ACTION)
    html = (
        b"<html><body><main><h1>Results</h1><table><tbody>"
        b"<tr><td>One bounded result</td></tr></tbody></table></main></body></html>"
    )
    fake_app = SimpleNamespace(
        DATABASE=SimpleNamespace(
            workflow_runs=SimpleNamespace(
                get_workflow_run=AsyncMock(return_value=run),
                get_workflow_run_output_parameters=AsyncMock(
                    return_value=[
                        SimpleNamespace(output_parameter_id="out_records", value=[{"name": "bounded result"}])
                    ]
                ),
            ),
            observer=SimpleNamespace(get_workflow_run_blocks=AsyncMock(return_value=[block])),
            workflows=SimpleNamespace(get_workflow_for_workflow_run=AsyncMock(return_value=run_workflow)),
            artifacts=SimpleNamespace(get_artifacts_for_run=AsyncMock(return_value=[artifact])),
        ),
        AGENT_FUNCTION=SimpleNamespace(should_dispatch_copilot_block_run_to_worker=AsyncMock(return_value=True)),
        ARTIFACT_MANAGER=SimpleNamespace(retrieve_artifact=AsyncMock(return_value=html)),
    )
    monkeypatch.setattr(run_execution_module, "app", fake_app)

    async def attach_trace(blocks: object, results: list[dict[str, object]], organization_id: str) -> None:
        results[0]["action_trace"] = [{"code_line": 7, "action": "evaluate"}]

    monkeypatch.setattr(run_execution_module, "_attach_action_traces", attach_trace)
    monkeypatch.setattr(run_execution_module, "_attach_failed_block_screenshots", AsyncMock())
    produced_outcomes: list[RecordedBuildTestOutcome] = []
    transported_outcomes: list[RecordedBuildTestOutcome | None] = []
    real_producer = run_execution_module.recorded_outcome_from_run_blocks_result
    real_packet_builder = run_execution_module.build_test_evidence_packet

    def capture_producer(*args: object, **kwargs: object) -> RecordedBuildTestOutcome:
        produced = real_producer(*args, **kwargs)
        produced_outcomes.append(produced)
        return produced

    def capture_packet(
        context: CopilotContext,
        result: object,
        *,
        recorded_outcome: RecordedBuildTestOutcome | None = None,
    ) -> BuildTestEvidencePacket:
        transported_outcomes.append(recorded_outcome)
        return real_packet_builder(context, result, recorded_outcome=recorded_outcome)

    monkeypatch.setattr(run_execution_module, "recorded_outcome_from_run_blocks_result", capture_producer)
    monkeypatch.setattr(run_execution_module, "build_test_evidence_packet", capture_packet)
    hydrated = await run_execution_module.hydrate_prior_run_packet(ctx, workflow_run_id="wr_failed_complete_packet")
    system_input = str(
        _build_dynamic_system_prompt("", CopilotConfig(block_authoring_policy=BlockAuthoringPolicy.CODE_ONLY_BROWSER))(
            SimpleNamespace(context=ctx), None
        )
    )
    ordinary_input = (
        system_input
        + "\n"
        + _build_user_context(
            workflow_yaml=ctx.workflow_yaml,
            chat_history_text="",
            global_llm_context="",
            debug_run_info_text=_prior_run_debug_text(hydrated),
            user_message="Repair the recorded run.",
        )
    )

    assert hydrated is not None
    assert len(produced_outcomes) == 1
    assert produced_outcomes[0].workflow_run_id == "wr_failed_complete_packet"
    assert transported_outcomes == [produced_outcomes[0]]
    assert ctx.latest_recorded_build_test_outcome is None
    assert '"status": "failed"' in ordinary_input
    assert '"workflow_run_id": "wr_failed_complete_packet"' in ordinary_input
    assert '"output_parameter_id": "out_records"' in ordinary_input
    assert '"output_parameter_key": "records"' in ordinary_input
    assert '"block_label": "collect_records"' in ordinary_input
    assert '"block_status": "failed"' in ordinary_input
    assert '"error_codes"' in ordinary_input
    assert '"user_code_error"' in ordinary_input
    assert '"failing_line": 7' in ordinary_input
    assert '"name": "bounded result"' in ordinary_input
    assert '"page_state"' in ordinary_input
    assert "One bounded result" in ordinary_input
    assert "RECORDED BUILD-TEST OUTCOME" not in system_input
    assert "repairable_failure" not in system_input
    assert "wr_failed_complete_packet" not in system_input


def test_unavailable_registered_output_rows_do_not_become_false_missing_output_facts() -> None:
    ctx = _locator_packet_ctx()
    result = {
        "ok": False,
        "data": {
            "workflow_run_id": "wr_registry_unavailable",
            "overall_status": "failed",
            "failure_type": "user_code_error",
            "blocks": [
                {
                    "label": "collect_records",
                    "status": "failed",
                    "failure_reason": "failed",
                    "error_codes": ["user_code_error"],
                }
            ],
            "requested_output_parameter_definitions": [
                {
                    "workflow_run_id": "wr_registry_unavailable",
                    "output_parameter_id": "out_records",
                    "output_parameter_key": "records",
                }
            ],
            "registered_output_values_omission": "persisted registered output values were unavailable",
        },
    }

    outcome = recorded_outcome_from_run_blocks_result(result)

    assert outcome is not None
    assert outcome.missing_requested_output_facts == []
    record_build_test_outcome(ctx, outcome)
    packet = project_build_test_packet_for_llm(build_test_evidence_packet(ctx, result))
    assert any("persisted registered output values were unavailable" in notice for notice in packet.omission_notices)


def test_runtime_block_failure_outcome_keys_playwright_hidden_locator_structure() -> None:
    table_result = {
        "ok": False,
        "data": {
            "workflow_run_id": "wr_failed",
            "overall_status": "failed",
            "blocks": [
                {
                    "label": "extract_records",
                    "status": "failed",
                    "failure_reason": (
                        "Failed to execute code block. Reason: TimeoutError: Locator.wait_for: "
                        'Timeout 15000ms exceeded.\nCall log:\nwaiting for locator("table").first '
                        "to be visible\n  -   locator resolved to hidden <table>...</table>\n"
                    ),
                }
            ],
        },
    }
    row_result = {
        "ok": False,
        "data": {
            "workflow_run_id": "wr_failed_again",
            "overall_status": "failed",
            "blocks": [
                {
                    "label": "extract_records",
                    "status": "failed",
                    "failure_reason": (
                        "Failed to execute code block. Reason: TimeoutError: Locator.wait_for: "
                        'Timeout 15000ms exceeded.\nCall log:\nwaiting for locator("table tbody tr").first '
                        "to be visible\n  -   locator resolved to hidden <tr>...</tr>\n"
                    ),
                }
            ],
        },
    }

    table_outcome = recorded_outcome_from_run_blocks_result(table_result)
    row_outcome = recorded_outcome_from_run_blocks_result(row_result)

    assert table_outcome is not None
    assert row_outcome is not None
    assert table_outcome.is_authoritative is True
    assert row_outcome.is_authoritative is True
    assert table_outcome.structural_key != row_outcome.structural_key
    assert table_outcome.key_provenance["structural_failure_identity"] == "typed runtime failure structure"


def test_persisted_run_prose_only_failure_is_not_authoritative() -> None:
    result = {
        "ok": False,
        "error": "The registry form failed after waiting for the same selector.",
        "data": {
            "workflow_run_id": "wr_failed",
            "overall_status": "failed",
            "failure_reason": "Timeout waiting for selector #results on the registry form.",
            "blocks": [{"label": "search_records", "status": "failed"}],
        },
    }

    outcome = recorded_outcome_from_run_blocks_result(result)

    assert outcome is None or outcome.is_authoritative is False
    assert outcome is None or outcome.structural_key is None


def _run_history_ctx(workflow_yaml: str) -> SimpleNamespace:
    return SimpleNamespace(
        workflow_yaml=workflow_yaml,
        # The delivered candidate defaults to what the turn is working on; a test that cares about a
        # draft diverging from the saved workflow sets them apart explicitly.
        persisted_workflow_yaml=workflow_yaml,
        staged_workflow_yaml=None,
        latest_recorded_build_test_outcome=None,
        recorded_build_test_outcome_history=[],
        recorded_persisted_block_run_workflow_run_id=None,
    )


def test_failure_stays_open_when_a_later_run_passes_without_taking_the_failed_branch() -> None:
    """The live shape: the block runs to completion while its internal branch skips the failing call."""
    ctx = _run_history_ctx(two_page_login_yaml())
    record_build_test_outcome(ctx, failed_second_factor_run("wr_1"))
    record_build_test_outcome(ctx, passing_run("wr_2", ["sign_in_and_read"]))

    open_failure = unresolved_runtime_block_failure(ctx)

    assert open_failure is not None
    assert open_failure.workflow_run_id == "wr_1"
    assert open_failure.block_label == "sign_in_and_read"


def test_a_later_run_of_a_straight_line_block_does_not_clear_the_failure() -> None:
    """Executing the block again proves the call ran, not that it met the condition that failed.

    A login step fails against an already-authenticated page and passes against a signed-out one on
    the same lines, so a later success on unchanged code establishes nothing about the failure.
    """
    ctx = _run_history_ctx(straight_line_login_yaml())
    record_build_test_outcome(ctx, failed_second_factor_run("wr_1"))
    record_build_test_outcome(ctx, passing_run("wr_2", ["sign_in_and_read"]))

    open_failure = unresolved_runtime_block_failure(ctx)
    assert open_failure is not None
    assert open_failure.workflow_run_id == "wr_1"


def test_a_later_run_of_a_branched_block_does_not_clear_the_failure() -> None:
    ctx = _run_history_ctx(two_page_login_yaml())
    record_build_test_outcome(ctx, failed_second_factor_run("wr_1"))
    record_build_test_outcome(ctx, passing_run("wr_2", ["sign_in_and_read"]))

    assert unresolved_runtime_block_failure(ctx) is not None


def test_a_delivered_candidate_that_removes_the_call_retires_the_failure() -> None:
    """The legitimate repair: the candidate this turn delivers no longer carries the failing call."""
    ctx = _run_history_ctx(two_page_login_yaml())
    record_build_test_outcome(ctx, failed_second_factor_run("wr_1"))
    record_build_test_outcome(ctx, passing_run("wr_2", ["sign_in_and_read"]))
    repaired = two_page_login_yaml(submit_selector="Continue")
    ctx.workflow_yaml = repaired

    assert unresolved_runtime_block_failure(ctx, reported_workflow_yaml=repaired) is None


def test_a_block_missing_from_the_delivered_workflow_does_not_clear_the_failure() -> None:
    """The delivered snapshot is taken before the turn authors anything, so a block written during
    the turn is absent from it. Absence is not proof of repair."""
    ctx = _run_history_ctx(two_page_login_yaml())
    record_build_test_outcome(ctx, failed_second_factor_run("wr_1"))
    record_build_test_outcome(ctx, passing_run("wr_2", ["sign_in_and_read"]))
    snapshot_without_the_block = "title: t\nworkflow_definition:\n  blocks: []\n"

    assert unresolved_runtime_block_failure(ctx, reported_workflow_yaml=snapshot_without_the_block) is not None


def test_an_unrelated_edit_to_the_block_does_not_retire_the_failing_call() -> None:
    """The call-level check earns its place here: the block changed, the failing call did not."""
    ctx = _run_history_ctx(two_page_login_yaml())
    record_build_test_outcome(ctx, failed_second_factor_run("wr_1"))
    record_build_test_outcome(ctx, passing_run("wr_2", ["sign_in_and_read"]))
    ctx.workflow_yaml = two_page_login_yaml().replace(
        'return {"visitors": "9.42K"}', 'return {"visitors": "9.42K", "extra": 1}'
    )

    open_failure = unresolved_runtime_block_failure(ctx)

    assert open_failure is not None
    assert open_failure.block_label == "sign_in_and_read"


def test_a_failure_with_no_later_run_is_the_turns_own_headline_not_an_unresolved_note() -> None:
    ctx = _run_history_ctx(two_page_login_yaml())
    record_build_test_outcome(ctx, failed_second_factor_run("wr_1"))

    assert unresolved_runtime_block_failure(ctx) is None


def test_another_record_for_the_same_run_id_is_not_a_later_run() -> None:
    ctx = _run_history_ctx(two_page_login_yaml())
    record_build_test_outcome(ctx, failed_second_factor_run("wr_1"))
    record_build_test_outcome(ctx, passing_run("wr_1", ["sign_in_and_read"]))

    assert unresolved_runtime_block_failure(ctx) is None


def test_author_time_work_after_the_failure_is_not_a_later_run() -> None:
    """Scout evaluations and author-time rejects share this history but execute nothing."""
    ctx = _run_history_ctx(two_page_login_yaml())
    record_build_test_outcome(ctx, failed_second_factor_run("wr_1"))
    record_build_test_outcome(
        ctx,
        RecordedBuildTestOutcome(
            phase="author_time_reject",
            attempted_tool="update_and_run_blocks",
            verdict="repairable_failure",
            reason_code="synthesized_parameter_binding_ambiguous",
            structural_failure_identity="author-time-identity",
        ),
    )

    assert unresolved_runtime_block_failure(ctx) is None


def test_a_later_run_still_arms_the_note_when_author_time_work_follows_it() -> None:
    ctx = _run_history_ctx(two_page_login_yaml())
    record_build_test_outcome(ctx, failed_second_factor_run("wr_1"))
    record_build_test_outcome(ctx, passing_run("wr_2", ["read_metric"]))
    record_build_test_outcome(
        ctx,
        RecordedBuildTestOutcome(
            phase="scout_evaluate",
            attempted_tool="evaluate",
            verdict="progress_observed",
            reason_code="run_completed_unevaluated",
            structural_failure_identity="scout-identity",
        ),
    )

    open_failure = unresolved_runtime_block_failure(ctx)

    assert open_failure is not None
    assert open_failure.workflow_run_id == "wr_1"


def test_a_code_block_nested_in_a_loop_is_still_found() -> None:
    """A failure inside a loop body must stay reportable, not vanish because the walk missed it."""
    ctx = _run_history_ctx(
        """
    title: Loop over rows
    workflow_definition:
      blocks:
      - block_type: for_loop
        label: each_row
        loop_blocks:
        - block_type: code
          label: sign_in_and_read
          code: |
            if await page.locator("#token").count():
                await page.get_by_role("button", name="Login", exact=True).click()
    """
    )
    record_build_test_outcome(ctx, failed_second_factor_run("wr_1"))
    record_build_test_outcome(ctx, passing_run("wr_2", ["each_row"]))

    open_failure = unresolved_runtime_block_failure(ctx)

    assert open_failure is not None
    assert open_failure.block_label == "sign_in_and_read"


def test_a_css_selector_failure_matches_the_locator_call_in_the_draft() -> None:
    """A failure that spells it "selector:" must still match code that spells it locator()."""
    ctx = _run_history_ctx(
        """
    title: Read the metric
    workflow_definition:
      blocks:
      - block_type: code
        label: sign_in_and_read
        code: |
          if await page.locator("#token").count():
              await page.locator("#submit-btn").click()
    """
    )
    # Derived, not hardcoded: the point of the test is that the two spellings normalize together.
    call_ref = selector_identity_from_failure('TimeoutError: resolved selector: "#submit-btn" never appeared')
    assert call_ref == "locator:#submit-btn"
    outcome = failed_second_factor_run("wr_1").model_copy(update={"attempted_call_ref": call_ref})
    record_build_test_outcome(ctx, outcome)
    record_build_test_outcome(ctx, passing_run("wr_2", ["sign_in_and_read"]))

    open_failure = unresolved_runtime_block_failure(ctx)

    assert open_failure is not None
    assert open_failure.block_label == "sign_in_and_read"


def test_run_blocks_outcome_extracts_the_implicated_call_from_the_failure_text() -> None:
    outcome = recorded_outcome_from_run_blocks_result(
        {
            "ok": False,
            "data": {
                "workflow_run_id": "wr_failed",
                "overall_status": "failed",
                "failure_type": "runtime_error",
                "blocks": [
                    {
                        "label": "get_visitors",
                        "block_type": "code",
                        "status": "failed",
                        "failure_reason": (
                            "code block failed. failure reason: Failed to execute code block. Reason: "
                            "TimeoutError: Locator.click: Timeout 30000ms exceeded. Call log: - waiting for "
                            'get_by_role("button", name="Continue", exact=True)'
                        ),
                    },
                ],
            },
        },
    )

    assert outcome is not None
    assert outcome.attempted_call_ref == "role:button:Continue"


def _templated_selector_yaml(*, templated: bool = True) -> str:
    call = 'f"#submit-{kind}"' if templated else '"#submit-btn"'
    return f"""
    title: Read the metric
    workflow_definition:
      blocks:
      - block_type: code
        label: sign_in_and_read
        code: |
          if await page.locator("#token").count():
              await page.locator({call}).click()
    """


def test_a_templated_selector_does_not_read_as_the_call_being_removed() -> None:
    """A runtime-built selector is invisible to the literal scan, so it cannot prove removal."""
    ctx = _run_history_ctx(_templated_selector_yaml())
    outcome = failed_second_factor_run("wr_1").model_copy(update={"attempted_call_ref": "locator:#submit-btn"})
    record_build_test_outcome(ctx, outcome)
    record_build_test_outcome(ctx, passing_run("wr_2", ["sign_in_and_read"]))

    open_failure = unresolved_runtime_block_failure(ctx)

    assert open_failure is not None
    assert open_failure.block_label == "sign_in_and_read"


def test_an_all_literal_block_still_retires_when_the_call_is_gone() -> None:
    """The pass path: with every selector a literal, absence really is proof of removal."""
    delivered = _templated_selector_yaml(templated=False)
    ctx = _run_history_ctx(delivered)
    outcome = failed_second_factor_run("wr_1").model_copy(update={"attempted_call_ref": "locator:#gone-btn"})
    record_build_test_outcome(ctx, outcome)
    record_build_test_outcome(ctx, passing_run("wr_2", ["sign_in_and_read"]))

    assert unresolved_runtime_block_failure(ctx, reported_workflow_yaml=delivered) is None


def _secure_runner_failure_result(error_code: str, category: str | None) -> dict[str, object]:
    data: dict[str, object] = {
        "workflow_run_id": "wr_runner",
        "overall_status": "failed",
        "blocks": [
            {
                "label": "run_code",
                "block_type": "CODE",
                "status": "failed",
                "failure_reason": "Secure CodeBlock runner is unavailable. Please retry.",
                "error_codes": [error_code],
            }
        ],
    }
    if category is not None:
        data["failure_categories"] = [{"category": category, "confidence_float": 1.0, "reasoning": "sandbox"}]
    return {"ok": False, "data": data}


def test_unrecoverable_tool_error_run_is_not_repairable_and_not_authoritative() -> None:
    outcome = recorded_outcome_from_run_blocks_result(
        _secure_runner_failure_result("runner_unavailable", "UNRECOVERABLE_TOOL_ERROR")
    )

    assert outcome is not None
    assert outcome.verdict != "repairable_failure"
    assert outcome.verdict == "not_authoritative"
    assert outcome.reason_code == "unrecoverable_tool_error"
    assert outcome.is_authoritative is False
    assert outcome.structural_failure_identity == ""


def test_user_code_error_run_stays_repairable() -> None:
    outcome = recorded_outcome_from_run_blocks_result(
        _secure_runner_failure_result("user_code_error", "CODE_BLOCK_FAILURE")
    )

    assert outcome is not None
    assert outcome.verdict == "repairable_failure"
    assert outcome.reason_code == "runtime_block_failure"


def test_runner_timeout_run_stays_repairable() -> None:
    outcome = recorded_outcome_from_run_blocks_result(_secure_runner_failure_result("timeout", "CODE_BLOCK_FAILURE"))

    assert outcome is not None
    assert outcome.verdict == "repairable_failure"
    assert outcome.reason_code == "runtime_block_failure"


def _failed_run_result(observations: object) -> dict[str, object]:
    data: dict[str, object] = {
        "workflow_run_id": "wr_1",
        "overall_status": "failed",
        "requested_block_labels": ["read_value"],
        "executed_block_labels": ["read_value"],
        "blocks": [{"label": "read_value", "status": "failed", "failure_reason": "CodeBlock execution timed out."}],
    }
    if observations is not None:
        data["authored_locator_observations"] = observations
    return {"ok": False, "data": data}


def _locator_packet_ctx() -> CopilotContext:
    return CopilotContext(
        organization_id="org",
        workflow_id="w",
        workflow_permanent_id="wpid",
        workflow_yaml="workflow_definition:\n  blocks: []\n",
        persisted_workflow_yaml="workflow_definition:\n  blocks: []\n",
        browser_session_id=None,
        stream=None,  # type: ignore[arg-type]
        api_key=None,
    )


def _oversized_packet(packet: BuildTestEvidencePacket) -> BuildTestEvidencePacket:
    filler = "f" * 200
    return packet.model_copy(
        update={
            "attempted_block_labels": [f"{filler}{index}" for index in range(30)],
            "executed_block_labels": [f"{filler}{index}" for index in range(30)],
            "registered_outputs": [
                BuildTestPacketRegisteredOutput(
                    workflow_run_id=filler,
                    output_parameter_id=filler,
                    output_parameter_key=filler,
                    block_label=filler,
                    block_type=filler,
                    value="v" * 1_000,
                )
                for _ in range(15)
            ],
            "requested_outputs": [
                BuildTestPacketRequestedOutput(
                    workflow_run_id="wr_failed_complete_packet",
                    output_parameter_id=f"requested_{index}",
                    output_parameter_key=f"requested_{index}",
                )
                for index in range(10)
            ],
            "downloads": [BuildTestPacketDownload(artifact_id=filler, file_name=filler) for _ in range(15)],
            "unfinished_items": [
                BuildTestPacketUnfinishedItem(
                    kind="unverified_block",
                    label=filler,
                    output_path=filler,
                    reason_code=filler,
                )
                for _ in range(30)
            ],
        }
    )


def test_a_standalone_clickable_control_reaches_the_packet_page_state_and_the_llm_projection() -> None:
    ctx = _locator_packet_ctx()
    ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
    result: dict[str, object] = {
        "ok": False,
        "error": "Run failed.",
        "data": {
            "workflow_run_id": "wr_failed",
            "overall_status": "failed",
            "blocks": [
                {
                    "label": "open_statements",
                    "status": "failed",
                    "failure_reason": 'Timeout waiting for locator("#continue-btn-x9")',
                }
            ],
        },
    }
    _record_run_blocks_result(ctx, result)
    ctx.composition_page_evidence = {
        "workflow_run_id": "wr_failed",
        "observed_after_workflow_run": True,
        "source_tool": "inspect_page_for_composition",
        "current_url": "https://example.test/app/statements",
        "navigation_targets": [{"text": f"Section {index}", "selector": f"a.s{index}"} for index in range(6)],
        "clickable_controls": [
            {"text": "Continue to statements", "selector": "#continue-btn-x9", "tag": "button", "disabled": True}
        ],
    }
    inject_runtime_authoring_repair_context(ctx, result)
    finalized = ctx.last_code_authoring_repair_context
    assert isinstance(finalized, CodeAuthoringRepairContext)

    packet = build_test_evidence_packet(ctx, result)
    projected = project_build_test_packet_for_llm(packet)
    compacted = project_build_test_packet_for_llm(_oversized_packet(packet))

    assert packet.failure is not None and packet.failure.page_state is not None
    assert packet.run.workflow_run_id == "wr_failed"
    assert packet.failure.page_state.action_summaries == finalized.page_action_summaries
    assert projected.failure is not None and projected.failure.page_state is not None
    assert projected.failure.page_state.action_summaries == [
        "Continue to statements disabled",
        "Section 0",
        "Section 1",
        "Section 2",
        "Section 3",
    ]
    assert all("#continue-btn-x9" not in summary for summary in projected.failure.page_state.action_summaries)
    assert compacted.failure is not None and compacted.failure.page_state is not None
    assert any("repeated packet facts shortened further" in notice for notice in compacted.omission_notices)
    assert compacted.failure.page_state.action_summaries == ["Continue to statements disabled", "Section 0"]


def test_locator_observations_reach_the_failure_packet() -> None:
    result = _failed_run_result([{"authored_selector": "button.old", "match_count": 0}])

    packet = build_test_evidence_packet(_locator_packet_ctx(), result)

    assert packet.failure is not None
    observation = packet.failure.locator_observations[0]
    assert (observation.authored_selector, observation.match_count) == ("button.old", 0)
    assert observation.match_index is None
    assert observation.observed_candidates is None
    assert observation.unobserved_reason is None


def test_an_unattempted_observation_is_disclosed_rather_than_reported_as_no_locators() -> None:
    # A dispatched run, a non-code block, or a policy without inline execution never attempts the
    # observation. That is a different fact from "this block names no locator", and it is also not
    # evidence the page was unreachable.
    packet = build_test_evidence_packet(_locator_packet_ctx(), _failed_run_result(None))

    assert packet.failure is not None
    assert packet.failure.locator_observations == []
    assert any("no post-action locator observation was attempted" in notice for notice in packet.omission_notices)


def test_a_block_with_no_literal_locator_is_disclosed_as_such() -> None:
    packet = build_test_evidence_packet(_locator_packet_ctx(), _failed_run_result([]))

    assert packet.failure is not None
    assert any("names no literal locator" in notice for notice in packet.omission_notices)


def test_locators_the_run_could_not_be_asked_about_remain_typed_rows() -> None:
    result = _failed_run_result(
        [
            {"authored_selector": "#rate", "unobserved_reason": "run_page_unavailable"},
            {"authored_selector": "text=Submit", "unobserved_reason": "run_page_unavailable"},
        ]
    )

    packet = build_test_evidence_packet(_locator_packet_ctx(), result)

    assert packet.failure is not None
    assert [row.authored_selector for row in packet.failure.locator_observations] == ["#rate", "text=Submit"]
    assert {row.unobserved_reason for row in packet.failure.locator_observations} == {"run_page_unavailable"}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"match_count": None},
        {"match_count": False},
        {"match_count": 0, "match_index": 0},
        {"match_count": 0, "observed_candidates": ["button"]},
        {"match_count": 2, "match_index": 0},
        {"match_count": 2, "observed_candidates": ["button"]},
        {"match_count": 2, "match_index": 0, "observed_candidates": [""]},
        {
            "match_count": 2,
            "match_index": 0,
            "observed_candidates": ["button"],
            "unobserved_reason": "identity_read_failed",
        },
        {"unobserved_reason": "run_page_unavailable", "observed_candidates": ["button"]},
    ],
)
def test_locator_observation_schema_rejects_mixed_or_incomplete_states(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        BuildTestPacketLocatorObservation.model_validate({"authored_selector": "button", **kwargs})


def test_positive_locator_observation_requires_index_zero_and_an_identity() -> None:
    row = BuildTestPacketLocatorObservation(
        authored_selector="button",
        match_count=2,
        match_index=0,
        observed_candidates=["button#save"],
    )

    assert row.match_count == 2
    assert row.match_index == 0
    assert row.observed_candidates == ["button#save"]


def test_a_malformed_observation_is_dropped_and_counted() -> None:
    result = _failed_run_result([{"authored_selector": "button.old", "match_count": "many"}, {"no_selector": True}])

    packet = build_test_evidence_packet(_locator_packet_ctx(), result)

    assert packet.failure is not None
    assert packet.failure.locator_observations == []
    assert any("omitted 2 malformed item(s)" in notice for notice in packet.omission_notices)


def test_failed_block_code_reads_the_definition_not_the_run_rows() -> None:
    # run_blocks works with workflow_run_block rows, which carry status and output but no code, so
    # a producer that looked for code on them would silently never observe anything.
    workflow = SimpleNamespace(
        workflow_definition={
            "blocks": [
                {"label": "ok_block", "block_type": "code", "code": "pass"},
                {"label": "read_value", "block_type": "code", "code": 'page.locator("button.old")'},
            ]
        }
    )
    run_rows = [{"label": "ok_block", "status": "completed"}, {"label": "read_value", "status": "failed"}]

    assert _failed_block_code(workflow, _first_failed_result(run_rows)) == 'page.locator("button.old")'


def test_failed_block_code_uses_failed_result_row_order_not_definition_order() -> None:
    workflow = SimpleNamespace(
        workflow_definition={
            "blocks": [
                {"label": "main", "block_type": "code", "code": 'page.locator("#main")'},
                {"label": "finally", "block_type": "code", "code": 'page.locator("#finally")'},
            ]
        }
    )

    assert (
        _failed_block_code(
            workflow,
            _first_failed_result([{"label": "finally", "status": "failed"}, {"label": "main", "status": "failed"}]),
        )
        == 'page.locator("#finally")'
    )


def test_failed_block_code_does_not_skip_a_nullable_label_to_later_failed_row() -> None:
    workflow = SimpleNamespace(
        workflow_definition={
            "blocks": [
                {"label": "main", "block_type": "code", "code": 'page.locator("#main")'},
                {"label": "finally", "block_type": "code", "code": 'page.locator("#finally")'},
            ]
        }
    )
    failed_rows = [{"label": None, "status": "failed"}, {"label": "finally", "status": "failed"}]

    selected = _first_failed_result(failed_rows)

    assert selected is failed_rows[0]
    assert _failed_block_code(workflow, selected) is None


def test_nullable_label_sequence_keeps_packet_metadata_and_locator_evidence_on_selected_row() -> None:
    workflow = SimpleNamespace(
        workflow_definition={
            "blocks": [
                {"label": "finally", "block_type": "code", "code": 'page.locator("#finally")'},
            ]
        }
    )
    failed_rows = [
        {"label": None, "status": "failed", "failure_reason": "unlabeled failed"},
        {"label": "finally", "status": "failed", "failure_reason": "finally failed"},
    ]
    selected = _first_failed_result(failed_rows)
    result = _failed_run_result(None)
    data = result["data"]
    assert isinstance(data, dict)
    data["blocks"] = failed_rows
    data["action_trace_summary"] = ["unlabeled line 3 failed"]
    data["failing_code_line"] = 3

    packet = build_test_evidence_packet(_locator_packet_ctx(), result)

    assert _failed_block_code(workflow, selected) is None
    assert packet.failure is not None
    assert packet.failure.block_label is None
    assert packet.failure.reason == "unlabeled failed"
    assert packet.failure.action_trace == ["unlabeled line 3 failed"]
    assert packet.failure.failing_line == 3
    assert packet.failure.locator_observations == []


def test_failure_label_trace_and_locators_share_the_first_failed_result_row() -> None:
    result = _failed_run_result(
        [
            {
                "authored_selector": "#finally",
                "match_count": 1,
                "match_index": 0,
                "observed_candidates": ["button#finally"],
            }
        ]
    )
    data = result["data"]
    assert isinstance(data, dict)
    data["blocks"] = [
        {"label": "finally", "status": "failed", "failure_reason": "finally failed"},
        {"label": "main", "status": "failed", "failure_reason": "main failed"},
    ]
    data["action_trace_summary"] = ["finally line 7 failed"]
    data["failing_code_line"] = 7

    packet = build_test_evidence_packet(_locator_packet_ctx(), result)

    assert packet.failure is not None
    assert packet.failure.block_label == "finally"
    assert packet.failure.action_trace == ["finally line 7 failed"]
    assert packet.failure.failing_line == 7
    assert [row.authored_selector for row in packet.failure.locator_observations] == ["#finally"]


def test_failed_block_code_is_none_when_nothing_failed() -> None:
    workflow = SimpleNamespace(workflow_definition={"blocks": [{"label": "a", "block_type": "code", "code": "pass"}]})

    results = [{"label": "a", "status": "completed"}]
    assert _failed_block_code(workflow, _first_failed_result(results)) is None


def test_authored_literal_selectors_are_ordered_by_source_position() -> None:
    # ast.walk is breadth-first, so a nested locator would surface after a later shallow one.
    code = 'await wrapper(page.locator("first"))\npage.locator("second")\n'

    assert _authored_literal_locator_selectors(code) == ["first", "second"]


def test_a_run_outside_the_chats_browser_says_so() -> None:
    # A carried resume browser is detached without being freshly minted, so the fact is derived
    # from detachment: keying it on minting would tell the model it shared the chat's browser.
    result = _failed_run_result(None)
    result["data"]["run_detached_from_chat"] = True

    packet = build_test_evidence_packet(_locator_packet_ctx(), result)

    assert packet.run.browser is not None
    assert packet.run.browser.ran_outside_this_chats_browser is True
    assert "other than" in packet.run.browser.note


def test_a_run_sharing_the_chat_browser_says_that_instead() -> None:
    result = _failed_run_result(None)
    result["data"]["run_detached_from_chat"] = False

    packet = build_test_evidence_packet(_locator_packet_ctx(), result)

    assert packet.run.browser is not None
    assert packet.run.browser.ran_outside_this_chats_browser is False
    assert "target=" not in packet.run.browser.note


def test_an_unrecorded_browser_relationship_is_absent_rather_than_guessed() -> None:
    packet = build_test_evidence_packet(_locator_packet_ctx(), _failed_run_result(None))

    assert packet.run.browser is None


def test_the_runners_typed_error_and_failing_line_reach_the_repair_turn() -> None:
    # The runtime records which error code fired and which line raised. Rendering them into an
    # action-summary sentence tells the repair turn the block failed in English; these are the
    # fields it would act on.
    result = _failed_run_result(None)
    data = result["data"]
    assert isinstance(data, dict)
    data["blocks"] = [
        {
            "label": "extract_failure_rate",
            "status": "failed",
            "failure_reason": "code error at line 6",
            "error_codes": ["user_code_error"],
        }
    ]
    data["failing_code_line"] = 6

    packet = build_test_evidence_packet(_locator_packet_ctx(), result)

    assert packet.failure is not None
    assert packet.failure.error_codes == ["user_code_error"]
    assert packet.failure.failing_line == 6


def test_a_failure_the_runner_did_not_type_carries_neither_field() -> None:
    packet = build_test_evidence_packet(_locator_packet_ctx(), _failed_run_result(None))

    assert packet.failure is not None
    assert packet.failure.error_codes == []
    assert packet.failure.failing_line is None


def test_the_failing_line_is_read_from_the_recorders_own_stamp() -> None:
    # The cold-repair path derives the line from the attached action trace rather than parsing a
    # rendered sentence. Only the recorder's integer stamp counts: personalize_action writes user
    # data to other action fields.
    trace = [
        {"action": "NULL_ACTION", "status": "failed", "code_line": 6},
        {"action": "CLICK", "status": "completed"},
    ]

    assert _failing_code_line(trace) == 6


def test_a_trace_without_a_line_stamp_yields_none_rather_than_a_guess() -> None:
    assert _failing_code_line([{"action": "CLICK", "status": "failed"}]) is None
    assert _failing_code_line([{"action": "CLICK", "status": "failed", "code_line": "6"}]) is None
    assert _failing_code_line(None) is None


def test_the_projection_hands_every_consumer_an_already_redacted_packet() -> None:
    """This projection is the only redaction on the tool-result path, so a test that exercises the
    redactor rather than the projection would let a simplification delete it in silence."""
    from skyvern.forge.sdk.copilot.output_utils import project_build_test_packet_for_llm

    result = {
        "ok": False,
        "data": {
            "workflow_run_id": "wr_secret",
            "overall_status": "failed",
            "blocks": [
                {
                    "label": "extract",
                    "status": "failed",
                    "failure_reason": "login rejected with password=hunter2",
                    "error_codes": ["user_code_error"],
                }
            ],
        },
    }

    projected = project_build_test_packet_for_llm(build_test_evidence_packet(_locator_packet_ctx(), result))
    rendered = projected.model_dump_json()

    assert "hunter2" not in rendered
    assert "[REDACTED_SECRET]" in rendered
    assert projected.run.workflow_run_id == "wr_secret"
    assert projected.failure is not None
    assert projected.failure.block_label == "extract"
    assert projected.failure.error_codes == ["user_code_error"]


def test_a_draft_that_drops_the_call_does_not_clear_it_in_the_delivered_workflow() -> None:
    """Clearance must read the candidate the turn presents as saved, not whichever draft is current.

    A draft the user never receives can remove the failing call while the delivered workflow still
    contains it, which would clear a failure the customer still has.
    """
    ctx = _run_history_ctx(two_page_login_yaml())
    record_build_test_outcome(ctx, failed_second_factor_run("wr_1"))
    record_build_test_outcome(ctx, passing_run("wr_2", ["read_metric"]))
    # The draft under the cursor drops the failing call; the delivered workflow keeps it.
    ctx.workflow_yaml = two_page_login_yaml(submit_selector="Continue")
    delivered = two_page_login_yaml()
    ctx.persisted_workflow_yaml = delivered

    assert unresolved_runtime_block_failure(ctx, reported_workflow_yaml=delivered) is not None


def test_clearance_holds_when_the_delivered_workflow_dropped_the_call() -> None:
    """The legitimate case: the saved workflow itself no longer carries the failing call."""
    ctx = _run_history_ctx(two_page_login_yaml())
    record_build_test_outcome(ctx, failed_second_factor_run("wr_1"))
    record_build_test_outcome(ctx, passing_run("wr_2", ["read_metric"]))
    repaired = two_page_login_yaml(submit_selector="Continue")
    ctx.workflow_yaml = repaired
    ctx.persisted_workflow_yaml = repaired

    assert unresolved_runtime_block_failure(ctx, reported_workflow_yaml=repaired) is None


def _run_result_ok() -> dict:
    return {"ok": True, "data": {"workflow_run_id": "wr_2", "overall_status": "completed"}}


def test_a_passing_run_carries_an_earlier_unresolved_failure_to_the_model() -> None:
    """The model decides whether to repair from this result, so the failure has to survive into it.

    A later success otherwise displaces the failing run before that decision is made: the model sees a
    pass, has no record of the failure, and reports done.
    """
    from skyvern.forge.sdk.copilot.tools.run_execution import _carry_unresolved_failure_into_result

    ctx = _run_history_ctx(two_page_login_yaml())
    record_build_test_outcome(ctx, failed_second_factor_run("wr_1"))
    record_build_test_outcome(ctx, passing_run("wr_2", ["sign_in_and_read"]))
    result = _run_result_ok()

    _carry_unresolved_failure_into_result(ctx, result)

    carried = result["data"]["unresolved_earlier_failure"]
    assert carried["workflow_run_id"] == "wr_1"
    assert carried["block_label"] == "sign_in_and_read"
    assert carried["failure_kind"] == "runtime_block_failure"
    assert "does not establish" in carried["note"]


def test_a_delivered_repair_leaves_the_passing_result_untouched() -> None:
    from skyvern.forge.sdk.copilot.tools.run_execution import _carry_unresolved_failure_into_result

    repaired = two_page_login_yaml(submit_selector="Continue")
    ctx = _run_history_ctx(repaired)
    ctx.persisted_workflow_yaml = repaired
    record_build_test_outcome(ctx, failed_second_factor_run("wr_1"))
    record_build_test_outcome(ctx, passing_run("wr_2", ["sign_in_and_read"]))
    result = _run_result_ok()

    _carry_unresolved_failure_into_result(ctx, result)

    assert "unresolved_earlier_failure" not in result["data"]


def test_a_passing_run_with_no_earlier_failure_carries_no_extra_field() -> None:
    from skyvern.forge.sdk.copilot.tools.run_execution import _carry_unresolved_failure_into_result

    ctx = _run_history_ctx(two_page_login_yaml())
    record_build_test_outcome(ctx, passing_run("wr_2", ["sign_in_and_read"]))
    result = _run_result_ok()

    _carry_unresolved_failure_into_result(ctx, result)

    assert "unresolved_earlier_failure" not in result["data"]


def test_the_carry_survives_compaction_of_an_older_tool_output() -> None:
    """The model may decide to repair several turns later, after the output has been compacted.

    Dropping the field there would reproduce the loss it exists to prevent.
    """
    import json

    from skyvern.forge.sdk.copilot.enforcement import _summarize_tool_output

    carried = {
        "workflow_run_id": "wr_1",
        "block_label": "sign_in_and_read",
        "failure_kind": "runtime_block_failure",
        "note": "this run passing does not establish that the earlier failure was resolved",
    }
    output = json.dumps(
        {
            "ok": True,
            "data": {
                "workflow_run_id": "wr_2",
                "overall_status": "completed",
                "unresolved_earlier_failure": carried,
                "padding": "x" * 400,
            },
        }
    )

    synopsis = json.loads(_summarize_tool_output(output))

    assert synopsis["unresolved_earlier_failure"] == carried


def test_every_run_surface_attaches_through_the_same_helper() -> None:
    """One attachment helper covers every run surface; attaching at a single tool left it inert.

    Successful runs reach it from `run_blocks` and from the shared edit/update path, and some
    unsuccessful early returns call it harmlessly. What matters is that no run surface returns a
    result without passing through it.
    """
    from pathlib import Path as _Path

    tools = _Path(__file__).resolve().parents[2] / "skyvern/forge/sdk/copilot/tools/__init__.py"
    source = tools.read_text()

    call = "_carry_unresolved_failure_into_result(copilot_ctx, run_result, tool_name)"
    assert source.count(call) == 1
    shared = source.index(call)
    # The shared path records the run, then attaches, then hands the result to the model.
    window = source[shared - 400 : shared + 200]
    assert "_verify_and_record_run_blocks_result" in window
    assert "record_tool_step_result_for_ctx" in window


def test_the_carried_field_claims_only_that_the_pass_proves_nothing() -> None:
    """It reports a fact and prescribes no repair; the model still owns the branch."""
    from skyvern.forge.sdk.copilot.tools.run_execution import _carry_unresolved_failure_into_result

    ctx = _run_history_ctx(two_page_login_yaml())
    record_build_test_outcome(ctx, failed_second_factor_run("wr_1"))
    result = {"ok": True, "data": {"workflow_run_id": "wr_2", "overall_status": "completed"}}

    _carry_unresolved_failure_into_result(ctx, result)

    carried = result["data"]["unresolved_earlier_failure"]
    assert set(carried) == {"workflow_run_id", "block_label", "failure_kind", "note"}
    assert carried["note"] == "this run passing does not establish that the earlier failure was resolved"
    for forbidden in ("add", "guard", "should", "must", "conditional", "if "):
        assert forbidden not in carried["note"].lower(), "the note prescribes a fix"
