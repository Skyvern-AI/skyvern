from __future__ import annotations

from types import SimpleNamespace

from skyvern.forge.sdk.copilot.build_test_outcome import (
    RecordedBuildTestOutcome,
    authored_structure_signature_from_workflow,
    record_build_test_outcome,
    recorded_outcome_from_author_time_reject,
    recorded_outcome_from_authoring_repair_context,
    recorded_outcome_from_loaded_result_evidence,
    recorded_outcome_from_run_blocks_result,
)
from skyvern.forge.sdk.copilot.code_block_preflight import SANDBOX_UNRESOLVED_NAME_REASON_CODE
from skyvern.forge.sdk.copilot.completion_verification import CompletionVerificationResult, CriterionVerdict
from skyvern.forge.sdk.copilot.context import CodeAuthoringRepairContext
from skyvern.forge.sdk.copilot.result_evidence import LoadedResultCompositionEvidence, LoadedResultCompositionTarget
from skyvern.forge.sdk.copilot.run_outcome import RecordedRunOutcome


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
        reason_code="outcome_not_demonstrated",
        structural_failure_identity="completion:typed-outcome",
        page_evidence_refs=["origin:https://example.com", "result:#results rows=0"],
        authored_structure_signature="authored:first",
    )
    second = first.model_copy(update={"authored_structure_signature": "authored:second"})

    assert first.structural_key is not None
    assert first.structural_key == second.structural_key
    assert first.authored_structure_signature == "authored:first"
    assert second.authored_structure_signature == "authored:second"


def test_structural_key_does_not_require_fixture_slug_or_raw_transcript_text() -> None:
    outcome = recorded_outcome_from_loaded_result_evidence(
        LoadedResultCompositionEvidence(
            result_container_count=1,
            table_result_container_count=1,
            targets=(
                LoadedResultCompositionTarget(
                    selector="#results",
                    is_table=True,
                    row_count=3,
                    sample_rows=("synthetic row text that must not become identity",),
                    text_excerpt="synthetic transcript excerpt that must not become identity",
                    structure_signature="target-structure",
                ),
            ),
            structure_signature="result-structure",
        )
    )

    assert outcome.structural_key is not None
    key_payload = outcome.structural_key_payload
    assert "fixture" not in str(key_payload).lower()
    assert "synthetic row text" not in str(key_payload)
    assert "synthetic transcript excerpt" not in str(key_payload)


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
    ctx = SimpleNamespace(latest_recorded_build_test_outcome=None, recorded_build_test_outcome_history=[])
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
        reason_code="outcome_not_demonstrated",
        structural_failure_identity="completion:typed",
        authored_structure_signature=signature,
    ).model_dump(mode="json")

    assert signature is not None
    assert signature == same_structure
    assert "page.goto" not in str(dumped)
    assert "Find the exact provider" not in str(dumped)


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


def test_outcome_not_demonstrated_keeps_authoritative_unsatisfied_criteria_identity() -> None:
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
            reason_code="outcome_not_demonstrated",
            workflow_run_id="wr_partial",
        ),
        completion_verification=verification,
        authored_structure_signature="authored:partial-location-only",
    )

    assert outcome is not None
    assert outcome.phase == "persisted_block_run"
    assert outcome.reason_code == "outcome_not_demonstrated"
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


def test_authoring_repair_context_produces_structural_recorded_outcome() -> None:
    context = CodeAuthoringRepairContext(
        block_label="search_records",
        reason_code=SANDBOX_UNRESOLVED_NAME_REASON_CODE,
        unresolved_names=["confirmation_number", "row_text"],
        parameter_keys=["confirmation_number"],
        available_parameter_keys=["confirmation_number"],
        binding_candidates=["confirmation_number", "row_text"],
    )

    outcome = recorded_outcome_from_authoring_repair_context(context)

    assert outcome.phase == "author_time_reject"
    assert outcome.reason_code == "sandbox_unresolved_name"
    assert outcome.structural_key is not None
    assert (
        outcome.structural_key
        == recorded_outcome_from_authoring_repair_context(
            context.model_copy(update={"unresolved_names": ["row_text", "confirmation_number"]})
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
