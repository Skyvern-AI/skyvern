from __future__ import annotations

from types import SimpleNamespace

from skyvern.forge.sdk.copilot.build_test_outcome import (
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
from skyvern.forge.sdk.copilot.context import CodeAuthoringRepairContext
from skyvern.forge.sdk.copilot.failure_tracking import selector_identity_from_failure
from skyvern.forge.sdk.copilot.run_outcome import RecordedRunOutcome
from tests.unit.copilot_test_helpers import (
    failed_second_factor_run,
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


def test_a_straight_line_block_a_later_run_executed_is_re_exercised() -> None:
    """No branch can skip a call, so executing the block proves the failing call ran again."""
    ctx = _run_history_ctx(straight_line_login_yaml())
    record_build_test_outcome(ctx, failed_second_factor_run("wr_1"))
    record_build_test_outcome(ctx, passing_run("wr_2", ["sign_in_and_read"]))

    assert unresolved_runtime_block_failure(ctx) is None


def test_failure_retires_once_the_implicated_call_leaves_the_draft() -> None:
    ctx = _run_history_ctx(two_page_login_yaml())
    record_build_test_outcome(ctx, failed_second_factor_run("wr_1"))
    record_build_test_outcome(ctx, passing_run("wr_2", ["sign_in_and_read"]))
    ctx.workflow_yaml = two_page_login_yaml(submit_selector="Continue")

    assert unresolved_runtime_block_failure(ctx) is None


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
    ctx = _run_history_ctx(_templated_selector_yaml(templated=False))
    outcome = failed_second_factor_run("wr_1").model_copy(update={"attempted_call_ref": "locator:#gone-btn"})
    record_build_test_outcome(ctx, outcome)
    record_build_test_outcome(ctx, passing_run("wr_2", ["sign_in_and_read"]))

    assert unresolved_runtime_block_failure(ctx) is None


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
