from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from skyvern.forge import app as forge_app
from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.copilot import runtime
from skyvern.forge.sdk.copilot import tools as tools_module
from skyvern.forge.sdk.copilot.agent import (
    _build_dynamic_system_prompt,
    _build_user_context,
    _make_agent_result,
    _prior_run_debug_text,
    _recorded_build_test_outcome_prompt,
)
from skyvern.forge.sdk.copilot.build_test_connect_failure import build_test_connect_failure_sentence
from skyvern.forge.sdk.copilot.build_test_outcome import (
    _EXECUTED_CALL_REF_LIMIT,
    BuildTestConnectFailure,
    BuildTestEvidencePacket,
    BuildTestFailedOperation,
    BuildTestPacketDownload,
    BuildTestPacketFailure,
    BuildTestPacketLocatorObservation,
    BuildTestPacketRegisteredOutput,
    BuildTestPacketRequestedOutput,
    BuildTestPacketUnfinishedItem,
    RecordedBuildTestOutcome,
    authored_block_signatures_from_workflow,
    authored_structure_signature_from_workflow,
    bind_post_run_page_evidence,
    observed_value_extraction_scaffold_lines,
    record_build_test_outcome,
    recorded_outcome_from_author_time_reject,
    recorded_outcome_from_authoring_repair_context,
    recorded_outcome_from_run_blocks_result,
    recorded_outcome_from_scout_act_observe_hollow,
    unresolved_runtime_block_failure,
    unresolved_runtime_block_failure_with_disposition,
)
from skyvern.forge.sdk.copilot.completion_verification import CompletionVerificationResult, CriterionVerdict
from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy, CopilotConfig
from skyvern.forge.sdk.copilot.context import CodeAuthoringRepairContext, CopilotContext
from skyvern.forge.sdk.copilot.enforcement import _summarize_tool_output
from skyvern.forge.sdk.copilot.failure_tracking import selector_identity_from_failure
from skyvern.forge.sdk.copilot.output_utils import (
    _INTERNAL_RUN_OUTCOME_RECORDED_KEY,
    project_build_test_packet_for_llm,
    sanitize_tool_result_for_llm,
)
from skyvern.forge.sdk.copilot.run_outcome import RecordedRunOutcome
from skyvern.forge.sdk.copilot.runtime_authoring_repair import inject_runtime_authoring_repair_context
from skyvern.forge.sdk.copilot.secret_scrub import clear_session_scrub_values, register_secret_scrub_value
from skyvern.forge.sdk.copilot.terminal_envelope import assemble_terminal_envelope, render_terminal_message
from skyvern.forge.sdk.copilot.tools import run_execution as run_execution_module
from skyvern.forge.sdk.copilot.tools.composition_capture import store_post_run_page_evidence
from skyvern.forge.sdk.copilot.tools.run_execution import (
    _authored_literal_locator_selectors,
    _carry_unresolved_failure_into_result,
    _failed_block_code,
    _failing_code_line,
    _first_failed_result,
    _record_run_blocks_result,
    _recorded_run_block_result,
    _run_blocks_and_collect_debug,
    _verify_and_record_run_blocks_result,
    build_test_evidence_packet,
)
from skyvern.forge.sdk.copilot.workflow_yaml import runner_code_block_associations
from skyvern.forge.sdk.schemas.copilot_turn_outcome import ResponseKind, TurnOutcome, UnresolvedRuntimeFailure
from skyvern.forge.sdk.schemas.persistent_browser_sessions import PersistentBrowserSession
from skyvern.forge.sdk.workflow.models.block import CodeBlock
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter, ParameterType
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus
from skyvern.services import workflow_service as workflow_service_module
from skyvern.webeye.browser_artifacts import BrowserArtifacts
from tests.unit.copilot_test_helpers import (
    HANDBACK_WORKFLOW_YAML,
    count_record_and_send,
    failed_second_factor_run,
    handback_ctx,
    install_run_blocks_harness,
    make_copilot_ctx,
    make_stub_html_artifact,
    page_only_failed_block,
    passing_run,
    same_run_page_evidence,
    straight_line_login_yaml,
    terminal_extraction_block,
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


def test_registered_block_download_reaches_packet_without_parseable_artifact_evidence() -> None:
    ctx = make_copilot_ctx()
    ctx.registered_artifact_evidence = None
    result = {
        "ok": True,
        "data": {
            "workflow_run_id": "wr_1",
            "overall_status": "completed",
            "requested_block_labels": ["download_statement"],
            "executed_block_labels": ["download_statement"],
            "blocks": [
                {
                    "label": "download_statement",
                    "status": "completed",
                    "extracted_data": {"downloaded_file_artifact_ids": ["a_dl_9"]},
                }
            ],
        },
    }

    packet = build_test_evidence_packet(ctx, result)

    assert [(download.artifact_id, download.file_name) for download in packet.downloads] == [("a_dl_9", None)]


def test_packet_projects_every_recorded_block_output_in_row_order_and_scrubs_secrets() -> None:
    ctx = _locator_packet_ctx()
    ctx.browser_session_id = "pbs_recorded_outputs"
    register_secret_scrub_value(ctx, "private-value")
    try:
        result = {
            "ok": False,
            "data": {
                "workflow_run_id": "wr_recorded_outputs",
                "overall_status": "failed",
                "blocks": [
                    {
                        "label": "produce_rows",
                        "status": "completed",
                        "output": {"real_key": "private-value"},
                    },
                    {"label": "produce_rows", "status": "completed", "output": []},
                    {
                        "label": "use_rows",
                        "status": "failed",
                        "failure_reason": "missing output key",
                        "output": {"available_keys": ["real_key"]},
                    },
                ],
            },
        }

        packet = build_test_evidence_packet(ctx, result)
    finally:
        clear_session_scrub_values("pbs_recorded_outputs")

    assert [output.model_dump(mode="json", exclude_none=True) for output in packet.registered_outputs] == [
        {
            "label": "produce_rows",
            "status": "completed",
            "output": {"real_key": "[REDACTED_SECRET]"},
            "value_complete": True,
        },
        {
            "label": "produce_rows",
            "status": "completed",
            "output": [],
            "value_complete": True,
        },
        {
            "label": "use_rows",
            "status": "failed",
            "output": {"available_keys": ["real_key"]},
            "value_complete": True,
        },
    ]
    producer_values = [output.output for output in packet.registered_outputs if output.label == "produce_rows"]
    repair_signals = [output.output for output in packet.registered_outputs if output.label == "use_rows"]
    assert producer_values == [{"real_key": "[REDACTED_SECRET]"}, []]
    assert repair_signals == [{"available_keys": ["real_key"]}]
    assert (
        packet.omission_notices.count("registered_outputs redacted 1 item(s) containing registered secret values.") == 1
    )


def test_recorded_run_block_projection_keeps_falsey_and_null_outputs() -> None:
    def row(output: object) -> SimpleNamespace:
        return SimpleNamespace(
            label="produce_rows",
            block_type=SimpleNamespace(name="CODE"),
            status="completed",
            workflow_run_block_id="wrb_1",
            task_id=None,
            failure_reason=None,
            error_codes=[],
            output=output,
        )

    assert _recorded_run_block_result(row({}))["output"] == {}
    assert _recorded_run_block_result(row(""))["output"] == ""
    assert _recorded_run_block_result(row(None))["output"] is None


def test_connect_failure_projects_through_recorded_outcome_and_packet() -> None:
    ctx = make_copilot_ctx()
    ctx.workflow_yaml = "title: preserved draft"
    ctx.staged_workflow_yaml = "title: preserved draft"
    ctx.last_workflow_yaml = "title: preserved draft"
    failure = BuildTestConnectFailure(
        state="cdp_connect_failed",
        workflow_run_id="wr_1",
        workflow_run_block_id="wrb_1",
        task_id="tsk_1",
        browser_session_id="pbs_1",
    )
    result = {
        "ok": False,
        "error": "Build-test browser acquisition stopped: cdp_connect_failed.",
        "data": {
            "workflow_run_id": "wr_1",
            "overall_status": "setup_failed",
            "browser_session_id": "pbs_1",
            "requested_block_labels": ["open"],
            "executed_block_labels": [],
            "blocks": [],
            "build_test_connect_failure": failure.model_dump(mode="json"),
        },
    }

    outcome = recorded_outcome_from_run_blocks_result(result)
    assert outcome is not None
    assert outcome.connect_failure == failure
    assert outcome.observed_evidence_summary == build_test_connect_failure_sentence(failure)
    packet = build_test_evidence_packet(ctx, result, recorded_outcome=outcome)
    assert packet.failure is not None
    assert packet.failure.connect_failure == failure
    assert packet.canonical_workflow_yaml == "title: preserved draft"


def test_connect_failure_clears_when_a_later_real_run_records_recovery() -> None:
    ctx = _run_history_ctx(two_page_login_yaml())
    connect_failure = RecordedBuildTestOutcome(
        phase="persisted_block_run",
        attempted_tool="update_and_run_blocks",
        verdict="not_authoritative",
        reason_code="unrecoverable_tool_error",
        workflow_run_id="wr_failed_connect",
        structural_failure_identity="build_test_connect:cdp_connect_failed",
        connect_failure=BuildTestConnectFailure(
            state="cdp_connect_failed",
            browser_session_id="pbs_failed_connect",
        ),
    )

    record_build_test_outcome(ctx, connect_failure)
    record_build_test_outcome(ctx, passing_run("wr_recovered", ["sign_in_and_read"]))

    assert ctx.latest_recorded_build_test_outcome is not None
    assert ctx.latest_recorded_build_test_outcome.workflow_run_id == "wr_recovered"
    assert ctx.latest_recorded_build_test_outcome.connect_failure is None
    assert ctx.recorded_build_test_outcome_history[-1]["connect_failure"] is None


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
        workflow_run_block_id="wrb_failed_complete_packet",
        task_id=None,
        label="collect_records",
        block_type=SimpleNamespace(name="CODE"),
        status="failed",
        failure_reason="NameError at generated line 7",
        error_codes=["user_code_error"],
        output=[{"name": "bounded result"}],
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
                        SimpleNamespace(output_parameter_id="out_snapshot", value=[{"name": "bounded result"}])
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
    assert hydrated["registered_outputs"] == [
        {
            "label": "collect_records",
            "status": "failed",
            "output": [{"name": "bounded result"}],
            "value_complete": True,
        }
    ]
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


def _raising_code_workflow_yaml(code: str) -> str:
    return (
        "workflow_definition:\n"
        "  parameters: []\n"
        "  blocks:\n"
        "  - block_type: code\n"
        "    label: book_trip\n"
        "    code: |\n" + "".join(f"      {line}\n" for line in code.splitlines())
    )


def _generated_code_exception_result(
    workflow_run_id: str,
    failure_reason: str,
    downloaded_file_artifact_ids: list[str] | None = None,
) -> dict[str, object]:
    block: dict[str, object] = {
        "label": "book_trip",
        "block_type": "CODE",
        "status": "failed",
        "workflow_run_block_id": "wrb_trip",
        "failure_reason": failure_reason,
        "error_codes": ["user_code_error"],
    }
    if downloaded_file_artifact_ids:
        block["extracted_data"] = {"downloaded_file_artifact_ids": downloaded_file_artifact_ids}
    return {
        "ok": False,
        "data": {
            "workflow_run_id": workflow_run_id,
            "browser_session_id": "pbs_trip",
            "overall_status": "failed",
            "requested_block_labels": ["book_trip"],
            "executed_block_labels": ["book_trip"],
            # The run reached the page but retained no usable evidence of it, which is the shape
            # that used to erase the whole outcome.
            "post_run_page_capture": {"status": "unavailable", "omission": "page_capture_unavailable"},
            "blocks": [block],
        },
    }


def test_generated_code_exception_reaches_repair_and_needs_a_changed_submission() -> None:
    """A raised exception is the only evidence: no page state, no outputs, no failure categories."""
    raising_code = "total = passenger_count * fare\nawait page.locator('#pay-now').click()\n"
    repaired_code = (
        "total = int(await page.locator('#pax').input_value()) * fare\nawait page.locator('#pay-now').click()\n"
    )
    failure_reason = "CodeBlock failed with NameError at line 1: name 'passenger_count' is not defined."
    other_line_reason = "CodeBlock failed with NameError at line 2: name 'passenger_count' is not defined."

    ctx = CopilotContext(
        organization_id="org",
        workflow_id="w",
        workflow_permanent_id="wpid",
        workflow_yaml=_raising_code_workflow_yaml(raising_code),
        persisted_workflow_yaml=_raising_code_workflow_yaml(raising_code),
        browser_session_id=None,
        stream=None,  # type: ignore[arg-type]
        api_key=None,
        block_authoring_policy=BlockAuthoringPolicy.CODE_ONLY_BROWSER,
    )
    outcome = recorded_outcome_from_run_blocks_result(
        _generated_code_exception_result("wr_trip", failure_reason),
        recorded_run_outcome=RecordedRunOutcome(
            verdict="not_demonstrated",
            reason_code="blocker_reported",
            display_reason=failure_reason,
            workflow_run_id="wr_trip",
            run_completed=False,
        ),
    )

    assert outcome is not None
    assert outcome.verdict == "repairable_failure"
    assert outcome.reason_code == "runtime_block_failure"
    assert outcome.attempted_block_label == "book_trip"
    assert outcome.is_authoritative is True
    assert failure_reason in outcome.observed_evidence_summary
    assert outcome.page_capture is not None and outcome.page_capture.omission == "page_capture_unavailable"

    # Two exceptions in the same block are different failures, not one repeated one.
    other_line_outcome = recorded_outcome_from_run_blocks_result(
        _generated_code_exception_result("wr_trip_again", other_line_reason)
    )
    assert other_line_outcome is not None
    assert other_line_outcome.structural_key != outcome.structural_key

    # The exception's own message is written by the failing code, so it cannot decide identity.
    # These two ran out of the same unlocatable raise; only the value the message quotes differs.
    unlocated = "CodeBlock failed with NameError: name 'passenger_count' is not defined."
    unlocated_quoting_a_line = "CodeBlock failed with NameError: name 'passenger_count' is not defined at line 9."
    assert (
        recorded_outcome_from_run_blocks_result(
            _generated_code_exception_result("wr_trip_third", unlocated_quoting_a_line)
        ).structural_key  # type: ignore[union-attr]
        == recorded_outcome_from_run_blocks_result(
            _generated_code_exception_result("wr_trip_fourth", unlocated)
        ).structural_key  # type: ignore[union-attr]
    )

    record_build_test_outcome(ctx, outcome)
    rendered = _recorded_build_test_outcome_prompt(ctx)
    assert failure_reason in rendered
    assert "page_capture: status=unavailable; omission=page_capture_unavailable" in rendered

    # Re-running the same code does not clear the failure; a changed submission does.
    assert unresolved_runtime_block_failure(
        ctx,
        reported_workflow_yaml=_raising_code_workflow_yaml(raising_code),
        pending_later_run_id="wr_trip_rerun",
    ) == UnresolvedRuntimeFailure(workflow_run_id="wr_trip", block_label="book_trip")
    assert (
        unresolved_runtime_block_failure(
            ctx,
            reported_workflow_yaml=_raising_code_workflow_yaml(repaired_code),
            pending_later_run_id="wr_trip_rerun",
        )
        is None
    )


def _looped_code_workflow_yaml(code: str) -> str:
    return (
        "workflow_definition:\n"
        "  parameters: []\n"
        "  blocks:\n"
        "  - block_type: for_loop\n"
        "    label: per_passenger\n"
        "    loop_over: passengers\n"
        "    loop_blocks:\n"
        "    - block_type: code\n"
        "      label: book_trip\n"
        "      code: |\n" + "".join(f"        {line}\n" for line in code.splitlines())
    )


def test_a_repaired_code_block_inside_a_loop_clears_its_failure() -> None:
    """A nested block is repaired by editing its code, so its signature has to be readable there."""
    raising_code = "total = passenger_count * fare\n"
    repaired_code = "total = int(await page.locator('#pax').input_value()) * fare\n"
    failure_reason = "CodeBlock failed with NameError at line 1: name 'passenger_count' is not defined."

    ctx = _run_history_ctx(_looped_code_workflow_yaml(raising_code))
    record_build_test_outcome(
        ctx,
        recorded_outcome_from_run_blocks_result(_generated_code_exception_result("wr_loop", failure_reason)),
    )

    assert ctx.recorded_build_test_outcome_history[-1]["attempted_block_signature"]
    assert unresolved_runtime_block_failure(
        ctx,
        reported_workflow_yaml=_looped_code_workflow_yaml(raising_code),
        pending_later_run_id="wr_loop_rerun",
    ) == UnresolvedRuntimeFailure(workflow_run_id="wr_loop", block_label="book_trip")
    assert (
        unresolved_runtime_block_failure(
            ctx,
            reported_workflow_yaml=_looped_code_workflow_yaml(repaired_code),
            pending_later_run_id="wr_loop_rerun",
        )
        is None
    )


def test_a_code_failure_with_no_typed_identity_is_not_admitted() -> None:
    """Nothing names the failure, so no later edit could ever be shown to have addressed it."""
    assert (
        recorded_outcome_from_run_blocks_result(
            {
                "ok": False,
                "data": {
                    "workflow_run_id": "wr_untyped",
                    "overall_status": "failed",
                    "requested_block_labels": ["book_trip"],
                    "blocks": [
                        {
                            "label": "book_trip",
                            "block_type": "CODE",
                            "status": "failed",
                            "failure_reason": "the block did not produce the requested output",
                        }
                    ],
                },
            }
        )
        is None
    )


def test_a_recorded_code_failure_always_has_a_route_out_and_native_blocks_stay_out() -> None:
    """Both edges of admitting a bare block failure.

    A non-builtin exception inside a code block reaches persistence as the runner's generic phrase,
    naming neither the exception nor a line, and that failure still has to clear when the block is
    rewritten. A native block — which has no code signature to change — must not be admitted at
    all, or its unresolved-failure note could never be cleared by any edit.
    """
    # Byte-for-byte what the runner persists when the raised class is not a builtin, e.g. a
    # Playwright timeout: the exception's own words are stripped on the way out.
    generic_reason = "CodeBlock failed while running user code."
    timed_out = 'await page.locator("#pay-now").click()\n'
    repaired_same_selector = 'await page.locator("#pay-now").first.click(timeout=15000)\n'

    ctx = _run_history_ctx(_raising_code_workflow_yaml(timed_out))
    record_build_test_outcome(
        ctx,
        recorded_outcome_from_run_blocks_result(
            {
                "ok": False,
                "data": {
                    "workflow_run_id": "wr_browser_op",
                    "overall_status": "failed",
                    "requested_block_labels": ["book_trip"],
                    "blocks": [
                        {
                            "label": "book_trip",
                            "block_type": "CODE",
                            "status": "failed",
                            "failure_reason": generic_reason,
                            "error_codes": ["user_code_error"],
                        }
                    ],
                },
            }
        ),
    )
    assert (
        unresolved_runtime_block_failure(
            ctx,
            reported_workflow_yaml=_raising_code_workflow_yaml(repaired_same_selector),
            pending_later_run_id="wr_browser_op_rerun",
        )
        is None
    )

    # A failure no rewrite could have prevented is not recorded as an authored-code failure: the
    # same block can pass unchanged once the sandbox is back, the browser reconnects, or the run is
    # not cancelled, and recording one would hold the block open until an unrelated edit.
    for code in ("runner_unavailable", "browser_disconnected", "cancelled"):
        assert (
            recorded_outcome_from_run_blocks_result(
                {
                    "ok": False,
                    "data": {
                        "workflow_run_id": f"wr_{code}",
                        "overall_status": "failed",
                        "requested_block_labels": ["book_trip"],
                        "blocks": [
                            {
                                "label": "book_trip",
                                "block_type": "CODE",
                                "status": "failed",
                                "failure_reason": generic_reason,
                                "error_codes": [code],
                            }
                        ],
                    },
                }
            )
            is None
        ), code

    native_outcome = recorded_outcome_from_run_blocks_result(
        {
            "ok": False,
            "data": {
                "workflow_run_id": "wr_native",
                "overall_status": "failed",
                "requested_block_labels": ["inspect_record"],
                "blocks": [
                    {
                        "label": "inspect_record",
                        "block_type": "TASK",
                        "status": "failed",
                        "failure_reason": "Reached the maximum steps (30)",
                        "error_codes": ["max_steps_exceeded"],
                    }
                ],
            },
        }
    )
    assert native_outcome is None


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
            "canonical_workflow_yaml": "w" * 30_000,
            "attempted_block_labels": [f"{filler}{index}" for index in range(30)],
            "executed_block_labels": [f"{filler}{index}" for index in range(30)],
            "registered_outputs": [
                BuildTestPacketRegisteredOutput(
                    label=filler,
                    status=filler,
                    output="v" * 1_000,
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


def test_screenshot_ablation_replays_preserve_page_facts_without_minting_a_decision() -> None:
    """A missing frame changes only capture facts; the recorded run's failure remains a run fact."""

    def rendered_bytes(value: object) -> bytes:
        return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()

    def replay(*, screenshot_present: bool) -> tuple[dict[str, object], dict[str, object], str]:
        ctx = _locator_packet_ctx()
        page_evidence: dict[str, object] = {
            "workflow_run_id": "wr_capture_failure",
            "source_browser_session_id": "pbs_capture_failure",
            "run_browser_session_id": "pbs_capture_failure",
            "observed_after_workflow_run": True,
            "source_tool": "inspect_page_for_composition",
            "current_url": "https://example.test/checkout/payment",
            "forms": [{"submit_controls": [{"text": "Place order", "disabled": False}]}],
            "clickable_controls": [{"text": "Continue to payment", "disabled": False}],
            **({} if screenshot_present else {"visual_capture_omissions": ["screenshot_capture_failed"]}),
        }
        result: dict[str, object] = {
            "ok": False,
            "data": {
                "workflow_run_id": "wr_capture_failure",
                "browser_session_id": "pbs_capture_failure",
                "overall_status": "failed",
                "requested_block_labels": ["continue_to_payment"],
                "executed_block_labels": ["continue_to_payment"],
                "blocks": [
                    {
                        "label": "continue_to_payment",
                        "status": "failed",
                        "failure_reason": "Click did not reach payment options.",
                    }
                ],
                "post_run_page_evidence": page_evidence,
                **({"screenshot_base64": "c2NyZWVuc2hvdA=="} if screenshot_present else {}),
            },
        }
        stored, preserved = store_post_run_page_evidence(
            ctx,
            page_evidence,
            run_id="wr_capture_failure",
            current_url="https://example.test/checkout/payment",
            source_browser_session_id="pbs_capture_failure",
            run_browser_session_id="pbs_capture_failure",
        )
        assert preserved is False
        assert stored["observed_after_workflow_run"] is True
        result["data"]["post_run_page_evidence"] = run_execution_module._same_run_page_evidence_for_result(
            ctx, "wr_capture_failure"
        )
        outcome = recorded_outcome_from_run_blocks_result(
            result,
            page_evidence=result["data"]["post_run_page_evidence"],
        )
        assert outcome is not None
        packet = project_build_test_packet_for_llm(
            build_test_evidence_packet(ctx, result, recorded_outcome=outcome)
        ).model_dump(mode="json", exclude_none=True)
        ordinary_input = _build_user_context(
            workflow_yaml=ctx.workflow_yaml,
            chat_history_text="",
            global_llm_context="",
            debug_run_info_text=_prior_run_debug_text(packet),
            user_message="Repair the recorded run.",
        )
        return outcome.model_dump(mode="json"), packet, ordinary_input

    with_screenshot = [replay(screenshot_present=True) for _ in range(3)]
    without_screenshot = [replay(screenshot_present=False) for _ in range(3)]
    assert len({rendered_bytes(replay_outcome) for replay_outcome, _, _ in with_screenshot}) == 1
    assert len({rendered_bytes(replay_outcome) for replay_outcome, _, _ in without_screenshot}) == 1
    assert len({rendered_bytes(packet) for _, packet, _ in with_screenshot}) == 1
    assert len({rendered_bytes(packet) for _, packet, _ in without_screenshot}) == 1

    baseline_outcome, baseline_packet, _ = with_screenshot[0]
    missing_outcome, missing_packet, ordinary_input = without_screenshot[0]
    assert {key: value for key, value in baseline_outcome.items() if key != "page_capture"} == {
        key: value for key, value in missing_outcome.items() if key != "page_capture"
    }
    assert missing_packet["page_capture"] == {"status": "captured", "omission": "screenshot_capture_failed"}
    assert baseline_packet["screenshot"]["present"] is True
    assert missing_packet["screenshot"]["present"] is False
    assert {
        key: value
        for key, value in baseline_packet.items()
        if key not in {"page_capture", "screenshot", "omission_notices"}
    } == {
        key: value
        for key, value in missing_packet.items()
        if key not in {"page_capture", "screenshot", "omission_notices"}
    }
    assert not {"success_verdict", "terminal", "repair_decision"}.intersection(missing_packet)
    decision_surface = {
        key: value
        for key, value in missing_packet.items()
        if key not in {"page_capture", "screenshot", "omission_notices"}
    }
    assert b"success_verdict" not in rendered_bytes(decision_surface)
    assert b"terminal" not in rendered_bytes(decision_surface)
    assert b"repair_decision" not in rendered_bytes(decision_surface)
    assert '"workflow_run_id": "wr_capture_failure"' in ordinary_input
    assert '"browser_session_id": "pbs_capture_failure"' in ordinary_input
    assert "Place order" in ordinary_input
    assert "Continue to payment" in ordinary_input
    assert '"screenshot_capture_failed"' in ordinary_input


def test_unavailable_page_capture_is_a_typed_omission_without_invented_page_state() -> None:
    result: dict[str, object] = {
        "ok": False,
        "data": {
            "workflow_run_id": "wr_page_unavailable",
            "overall_status": "failed",
            "failure_type": "runtime_error",
            "requested_block_labels": ["continue_to_payment"],
            "executed_block_labels": ["continue_to_payment"],
            "blocks": [
                {
                    "label": "continue_to_payment",
                    "status": "failed",
                    "failure_reason": "Click did not reach payment options.",
                }
            ],
            "post_run_page_capture": {"status": "unavailable", "omission": "page_capture_unavailable"},
        },
    }

    replayed_outcomes_and_packets = []
    for _ in range(3):
        outcome = recorded_outcome_from_run_blocks_result(result)
        assert outcome is not None
        packet = build_test_evidence_packet(_locator_packet_ctx(), result, recorded_outcome=outcome)
        replayed_outcomes_and_packets.append((outcome, packet))
    assert len({outcome.model_dump_json() for outcome, _ in replayed_outcomes_and_packets}) == 1
    assert len({packet.model_dump_json() for _, packet in replayed_outcomes_and_packets}) == 1
    outcome, packet = replayed_outcomes_and_packets[0]

    assert outcome.page_capture is not None
    assert outcome.page_capture.status == "unavailable"
    assert outcome.page_capture.omission == "page_capture_unavailable"
    assert packet.page_capture == outcome.page_capture
    assert packet.failure is not None and packet.failure.page_state is None
    assert packet.page_state is None
    assert "success_verdict" not in packet.model_dump_json()
    assert "repair_decision" not in packet.model_dump_json()
    assert "terminal" not in packet.model_dump_json()


def test_stale_page_evidence_cannot_infer_a_capture_for_the_current_run() -> None:
    result: dict[str, object] = {
        "ok": False,
        "data": {
            "workflow_run_id": "wr_current",
            "browser_session_id": "pbs_current",
            "overall_status": "failed",
            "failure_type": "runtime_error",
            "blocks": [{"label": "continue", "status": "failed", "failure_reason": "Click failed."}],
        },
    }
    stale_scout_packet = {
        "source_browser_session_id": "pbs_current",
        "observed_after_workflow_run": False,
        "forms": [{"submit_controls": [{"text": "Place order", "disabled": False}]}],
    }

    outcome = recorded_outcome_from_run_blocks_result(result, page_evidence=stale_scout_packet)
    assert outcome is not None
    packet = build_test_evidence_packet(_locator_packet_ctx(), result, recorded_outcome=outcome)

    assert outcome.page_capture is None
    assert outcome.page_evidence_refs == []
    assert packet.page_capture is None


def test_required_input_unbound_outcome_preserves_the_typed_capture_fact() -> None:
    result: dict[str, object] = {
        "ok": False,
        "data": {
            "workflow_run_id": "wr_unbound",
            "overall_status": "failed",
            "blocks": [{"label": "continue", "status": "failed", "failure_reason": "Input was not bound."}],
            "post_run_page_capture": {"status": "unavailable", "omission": "page_capture_unavailable"},
        },
    }

    outcome = recorded_outcome_from_run_blocks_result(
        result,
        unbound_required_parameter_keys=["checkout_id"],
        block_parameter_keys={"continue": ["checkout_id"]},
    )

    assert outcome is not None
    assert outcome.reason_code == "required_input_unbound"
    assert outcome.page_capture is not None
    assert outcome.page_capture.model_dump() == {"status": "unavailable", "omission": "page_capture_unavailable"}


@pytest.mark.parametrize(
    "return_path",
    [
        "terminal_challenge",
        "demonstrated",
        "not_evaluated",
        "no_structural_identity",
        "degraded_floor",
        "recorded_failed_block",
        "run_failed_block",
    ],
)
def test_every_run_outcome_return_path_preserves_the_typed_capture_fact(return_path: str) -> None:
    result: dict[str, object] = {
        "ok": False,
        "data": {
            "workflow_run_id": "wr_capture_paths",
            "overall_status": "failed",
            "failure_type": "runtime_error",
            "blocks": [{"label": "continue", "status": "failed", "failure_reason": "Click failed."}],
            "post_run_page_capture": {"status": "unavailable", "omission": "page_capture_unavailable"},
        },
    }
    recorded_run_outcome: RecordedRunOutcome | None = None
    completion_verification: CompletionVerificationResult | None = None
    if return_path == "terminal_challenge":
        recorded_run_outcome = RecordedRunOutcome(
            verdict="not_demonstrated",
            reason_code="terminal_challenge_blocker",
            workflow_run_id="wr_capture_paths",
        )
    elif return_path == "demonstrated":
        result = {**result, "ok": True, "data": {**result["data"], "blocks": []}}
        recorded_run_outcome = RecordedRunOutcome(verdict="demonstrated", workflow_run_id="wr_capture_paths")
    elif return_path == "not_evaluated":
        result = {**result, "ok": True, "data": {**result["data"], "blocks": []}}
        recorded_run_outcome = RecordedRunOutcome(verdict="not_evaluated", workflow_run_id="wr_capture_paths")
    elif return_path == "no_structural_identity":
        result = {**result, "ok": True, "data": {**result["data"], "blocks": [], "failure_type": None}}
        recorded_run_outcome = RecordedRunOutcome(
            verdict="not_demonstrated",
            reason_code="blocker_reported",
            workflow_run_id="wr_capture_paths",
        )
    elif return_path == "degraded_floor":
        result = {**result, "ok": True, "data": {**result["data"], "blocks": [], "failure_type": None}}
        recorded_run_outcome = RecordedRunOutcome(verdict="not_demonstrated", workflow_run_id="wr_capture_paths")
        completion_verification = CompletionVerificationResult(
            status="evaluated",
            criterion_ids=["c0"],
            verdicts=[CriterionVerdict(criterion_id="c0", state="unsatisfied", reason_code="no_evidence")],
            degraded_criterion_ids=["c0"],
        )
    elif return_path == "recorded_failed_block":
        recorded_run_outcome = RecordedRunOutcome(
            verdict="not_demonstrated",
            reason_code="blocker_reported",
            workflow_run_id="wr_capture_paths",
        )

    outcome = recorded_outcome_from_run_blocks_result(
        result,
        recorded_run_outcome=recorded_run_outcome,
        completion_verification=completion_verification,
    )

    assert outcome is not None
    assert outcome.page_capture is not None
    assert outcome.page_capture.model_dump() == {"status": "unavailable", "omission": "page_capture_unavailable"}


def test_page_capture_rejects_an_unavailable_page_without_its_typed_omission() -> None:
    with pytest.raises(ValidationError, match="page_capture_unavailable"):
        BuildTestEvidencePacket.model_validate(
            {
                "canonical_workflow_source": "unavailable",
                "run": {},
                "screenshot": {"present": False},
                "page_capture": {"status": "unavailable"},
            }
        )


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


def test_browser_operation_failure_projects_same_row_run_and_block_identity() -> None:
    result = _failed_run_result(None)
    data = result["data"]
    assert isinstance(data, dict)
    data["workflow_run_id"] = "wr_browser_operation"
    data["blocks"] = [
        {
            "workflow_run_block_id": "wrb_capture_failure",
            "label": "collect_failure_rate",
            "status": "failed",
            "failure_reason": "browser operation failed",
            "error_codes": ["browser_operation_failed"],
        }
    ]
    data["failing_code_line"] = 11

    outcome = recorded_outcome_from_run_blocks_result(result)
    packet = build_test_evidence_packet(_locator_packet_ctx(), result, recorded_outcome=outcome)

    assert outcome is not None
    assert outcome.failed_operation is not None
    assert outcome.failed_operation.model_dump() == {
        "kind": "browser_operation_failed",
        "workflow_run_id": "wr_browser_operation",
        "workflow_run_block_id": "wrb_capture_failure",
        "block_label": "collect_failure_rate",
        "failing_line": 11,
    }
    assert packet.failure is not None
    assert packet.failure.failed_operation == outcome.failed_operation
    projected = project_build_test_packet_for_llm(packet)
    assert projected.failure is not None
    assert projected.failure.failed_operation == outcome.failed_operation


def test_browser_operation_packet_uses_same_run_recorded_outcome_as_its_authority() -> None:
    result = _failed_run_result(None)
    data = result["data"]
    assert isinstance(data, dict)
    data["workflow_run_id"] = "wr_recorded_authority"
    data["blocks"] = [
        {
            "workflow_run_block_id": "wrb_recorded_authority",
            "label": "collect_failure_rate",
            "status": "failed",
            "error_codes": ["browser_operation_failed"],
        }
    ]
    outcome = recorded_outcome_from_run_blocks_result(result)
    assert outcome is not None
    assert outcome.failed_operation is not None

    blocks = data["blocks"]
    assert isinstance(blocks, list)
    assert isinstance(blocks[0], dict)
    blocks[0]["workflow_run_block_id"] = "wrb_raw_result_changed_after_recording"
    packet = build_test_evidence_packet(_locator_packet_ctx(), result, recorded_outcome=outcome)

    assert packet.failure is not None
    assert packet.failure.failed_operation == outcome.failed_operation
    assert packet.failure.failed_operation.workflow_run_block_id == "wrb_recorded_authority"


def test_browser_operation_producer_redacts_registered_block_label_secret() -> None:
    session_id = "pbs_build_test_operation_redaction"
    secret = "build-test-label-secret-value"
    ctx = SimpleNamespace(browser_session_id=session_id, secret_scrub_values=[])
    register_secret_scrub_value(ctx, secret)
    try:
        result = _failed_run_result(None)
        data = result["data"]
        assert isinstance(data, dict)
        data["blocks"] = [
            {
                "label": f"collect_{secret}_rate",
                "status": "failed",
                "error_codes": ["browser_operation_failed"],
            }
        ]
        outcome = recorded_outcome_from_run_blocks_result(result)
    finally:
        clear_session_scrub_values(session_id)

    assert outcome is not None
    assert outcome.failed_operation is not None
    assert secret not in outcome.model_dump_json()
    assert outcome.failed_operation.block_label == "collect_[REDACTED_SECRET]_rate"


def test_browser_operation_structural_key_ignores_transient_run_and_block_ids() -> None:
    first = _failed_run_result(None)
    first_data = first["data"]
    assert isinstance(first_data, dict)
    first_data["workflow_run_id"] = "wr_first"
    first_data["blocks"] = [
        {
            "workflow_run_block_id": "wrb_first",
            "label": "collect_failure_rate",
            "status": "failed",
            "error_codes": ["browser_operation_failed"],
        }
    ]
    first_data["failing_code_line"] = 11
    second = copy.deepcopy(first)
    second_data = second["data"]
    assert isinstance(second_data, dict)
    second_data["workflow_run_id"] = "wr_second"
    blocks = second_data["blocks"]
    assert isinstance(blocks, list)
    assert isinstance(blocks[0], dict)
    blocks[0]["workflow_run_block_id"] = "wrb_second"

    first_outcome = recorded_outcome_from_run_blocks_result(first)
    second_outcome = recorded_outcome_from_run_blocks_result(second)

    assert first_outcome is not None
    assert second_outcome is not None
    assert first_outcome.failed_operation is not None
    assert second_outcome.failed_operation is not None
    assert first_outcome.failed_operation.workflow_run_id != second_outcome.failed_operation.workflow_run_id
    assert first_outcome.structural_key == second_outcome.structural_key


def test_browser_operation_failure_survives_non_clearing_outcomes_until_the_block_completes() -> None:
    failed_workflow = """title: retain browser failure
workflow_definition:
  parameters: []
  blocks:
    - block_type: code
      label: collect_failure_rate
      code: |
        return {"failure_rate": await page.locator("canvas.failure-rate").inner_text()}
"""
    ctx = _locator_packet_ctx()
    ctx.workflow_yaml = failed_workflow
    ctx.persisted_workflow_yaml = failed_workflow
    ctx.runner_code_block_associations_by_label = {"collect_failure_rate": "cba_collect_failure_rate"}
    failed_operation = BuildTestFailedOperation(
        kind="browser_operation_failed",
        workflow_run_id="wr_failed",
        workflow_run_block_id="wrb_failed",
        block_label="collect_failure_rate",
        failing_line=1,
        block_association="cba_collect_failure_rate",
    )
    record_build_test_outcome(
        ctx,
        RecordedBuildTestOutcome(
            phase="persisted_block_run",
            attempted_tool="update_and_run_blocks",
            verdict="repairable_failure",
            reason_code="runtime_block_failure",
            workflow_run_id="wr_failed",
            block_labels=["collect_failure_rate"],
            requested_block_labels=["collect_failure_rate"],
            structural_failure_identity="browser-operation",
            failed_operation=failed_operation,
        ),
    )

    record_build_test_outcome(
        ctx,
        RecordedBuildTestOutcome(
            phase="scout_evaluate",
            attempted_tool="inspect_page_for_composition",
            verdict="progress_observed",
            reason_code="verified_success",
        ),
    )
    assert ctx.latest_recorded_build_test_outcome is not None
    assert ctx.latest_recorded_build_test_outcome.failed_operation == failed_operation

    record_build_test_outcome(
        ctx,
        RecordedBuildTestOutcome(
            phase="persisted_block_run",
            attempted_tool="update_and_run_blocks",
            verdict="progress_observed",
            reason_code="verified_success",
            workflow_run_id="wr_unchanged",
            block_labels=["collect_failure_rate"],
            requested_block_labels=["collect_failure_rate"],
            verified_progress_marker="run-completed",
        ),
    )
    assert ctx.latest_recorded_build_test_outcome is not None
    assert ctx.latest_recorded_build_test_outcome.failed_operation == failed_operation

    record_build_test_outcome(
        ctx,
        RecordedBuildTestOutcome(
            phase="persisted_block_run",
            attempted_tool="update_and_run_blocks",
            verdict="repairable_failure",
            reason_code="runtime_block_failure",
            workflow_run_id="wr_block_failed_again",
            block_labels=["collect_failure_rate"],
            requested_block_labels=["collect_failure_rate"],
            executed_block_labels=["collect_failure_rate"],
            executed_block_associations=("cba_collect_failure_rate",),
        ),
    )
    assert ctx.latest_recorded_build_test_outcome is not None
    assert ctx.latest_recorded_build_test_outcome.failed_operation == failed_operation

    record_build_test_outcome(
        ctx,
        RecordedBuildTestOutcome(
            phase="persisted_block_run",
            attempted_tool="update_and_run_blocks",
            verdict="not_authoritative",
            reason_code="run_completed_unevaluated",
            workflow_run_id="wr_block_completed",
            block_labels=["collect_failure_rate"],
            requested_block_labels=["collect_failure_rate"],
            executed_block_labels=["collect_failure_rate"],
            executed_block_associations=("cba_collect_failure_rate",),
            completed_block_associations=("cba_collect_failure_rate",),
        ),
    )
    assert ctx.latest_recorded_build_test_outcome is not None
    assert ctx.latest_recorded_build_test_outcome.failed_operation is None


def test_unevaluated_later_run_that_completed_the_failed_block_clears_the_stop() -> None:
    failed_workflow = """workflow_definition:
  parameters: []
  blocks:
    - block_type: code
      label: submit
      code: |
        await page.locator("#submit").click(timeout=1000)
"""
    ctx = _locator_packet_ctx()
    ctx.workflow_yaml = failed_workflow
    ctx.persisted_workflow_yaml = failed_workflow
    ctx.runner_code_block_associations_by_label = {"submit": "cba_submit"}
    record_build_test_outcome(
        ctx,
        RecordedBuildTestOutcome(
            phase="persisted_block_run",
            verdict="repairable_failure",
            reason_code="runtime_block_failure",
            workflow_run_id="wr_failed",
            structural_failure_identity="browser-operation",
            failed_operation=BuildTestFailedOperation(
                kind="browser_operation_failed",
                workflow_run_id="wr_failed",
                block_label="submit",
                failing_line=1,
                block_association="cba_submit",
            ),
        ),
    )
    record_build_test_outcome(
        ctx,
        RecordedBuildTestOutcome(
            phase="persisted_block_run",
            verdict="not_authoritative",
            reason_code="run_completed_unevaluated",
            workflow_run_id="wr_retested",
            executed_block_associations=("cba_submit",),
            completed_block_associations=("cba_submit",),
        ),
    )

    assert ctx.latest_recorded_build_test_outcome is not None
    assert ctx.latest_recorded_build_test_outcome.failed_operation is None


def test_browser_operation_failure_clears_when_a_full_replacement_completes_the_block() -> None:
    failed_workflow = """workflow_definition:
  parameters: []
  blocks:
    - block_type: code
      label: collect_failure_rate
      copilot_block_association: cba_original
      code: |
        return await page.locator("canvas.failure-rate").inner_text()
    - block_type: code
      label: summarize
      code: |
        return {"summary": "ready"}
"""
    replacement_workflow = """workflow_definition:
  parameters: []
  blocks:
    - block_type: code
      label: summarize
      code: |
        return {"summary": "ready"}
    - block_type: code
      label: collect_failure_rate
      copilot_block_association: cba_original
      code: |
        return {"unrelated": "replacement"}
"""
    ctx = _locator_packet_ctx()
    ctx.workflow_yaml = failed_workflow
    ctx.staged_workflow_yaml = failed_workflow
    ctx.runner_code_block_associations_by_label = {"collect_failure_rate": "cba_original"}
    failed_operation = BuildTestFailedOperation(
        kind="browser_operation_failed",
        workflow_run_id="wr_failed",
        block_label="collect_failure_rate",
        failing_line=1,
        block_association="cba_original",
    )
    record_build_test_outcome(
        ctx,
        RecordedBuildTestOutcome(
            phase="persisted_block_run",
            verdict="repairable_failure",
            reason_code="runtime_block_failure",
            workflow_run_id="wr_failed",
            structural_failure_identity="browser-operation",
            failed_operation=failed_operation,
        ),
    )

    ctx.workflow_yaml = replacement_workflow
    ctx.staged_workflow_yaml = replacement_workflow
    ctx.runner_code_block_associations_by_label = runner_code_block_associations(
        replacement_workflow,
        prior_associations=ctx.runner_code_block_associations_by_label,
        preserve_existing=False,
    )
    replacement_association = ctx.runner_code_block_associations_by_label["collect_failure_rate"]
    assert replacement_association != "cba_original"

    # Running the replacement without completing it resolves nothing.
    record_build_test_outcome(
        ctx,
        RecordedBuildTestOutcome(
            phase="persisted_block_run",
            verdict="not_authoritative",
            reason_code="runtime_block_failure",
            workflow_run_id="wr_replacement_failed",
            executed_block_labels=["collect_failure_rate"],
            executed_block_associations=(replacement_association,),
        ),
    )
    assert ctx.latest_recorded_build_test_outcome is not None
    assert ctx.latest_recorded_build_test_outcome.failed_operation == failed_operation

    record_build_test_outcome(
        ctx,
        RecordedBuildTestOutcome(
            phase="persisted_block_run",
            verdict="progress_observed",
            reason_code="verified_success",
            workflow_run_id="wr_replacement",
            executed_block_labels=["collect_failure_rate"],
            executed_block_associations=(replacement_association,),
            completed_block_associations=(replacement_association,),
            verified_progress_marker="run-completed",
        ),
    )

    # The replacement carries a fresh identity, so the recorded association can never run again;
    # the label's current identity completing is the only route by which this repair can clear.
    assert ctx.latest_recorded_build_test_outcome is not None
    assert ctx.latest_recorded_build_test_outcome.failed_operation is None


def test_code_block_association_covers_conditional_blocks_without_entering_packet_projection() -> None:
    workflow_yaml = """workflow_definition:
  parameters: []
  blocks:
    - block_type: conditional
      label: inspect_state
      branch_conditions:
        - condition: "True"
          blocks:
            - block_type: code
              label: read_failure_rate
              code: |
                return await page.locator("canvas.failure-rate").inner_text()
"""
    associations = runner_code_block_associations(workflow_yaml)

    assert set(associations) == {"read_failure_rate"}
    assert "copilot_block_association" not in workflow_yaml
    outcome = recorded_outcome_from_run_blocks_result(
        {
            "ok": False,
            "data": {
                "workflow_run_id": "wr_conditional_failure",
                "overall_status": "failed",
                "failing_code_line": 1,
                "blocks": [
                    {
                        "label": "read_failure_rate",
                        "status": "failed",
                        "error_codes": ["browser_operation_failed"],
                    }
                ],
            },
        },
        block_associations_by_label=associations,
    )

    assert outcome is not None
    assert outcome.failed_operation is not None
    assert outcome.failed_operation.block_association == associations["read_failure_rate"]
    assert "copilot_block_association" not in outcome.model_dump_json()


def test_recorded_outcome_execution_receipts_come_from_block_status_not_requested_labels() -> None:
    result = {
        "ok": False,
        "data": {
            "workflow_run_id": "wr_receipts",
            "overall_status": "failed",
            "requested_block_labels": ["requested_only", "executed"],
            "executed_block_labels": ["requested_only", "executed"],
            "blocks": [
                {"label": "requested_only", "status": "skipped"},
                {
                    "label": "executed",
                    "status": "failed",
                    "failure_reason": "browser operation failed",
                    "error_codes": ["browser_operation_failed"],
                },
            ],
        },
    }

    outcome = recorded_outcome_from_run_blocks_result(result)

    assert outcome is not None
    assert outcome.requested_block_labels == ["requested_only", "executed"]
    assert outcome.executed_block_labels == ["executed"]


def test_browser_operation_failure_without_a_block_association_is_never_cleared() -> None:
    failed_workflow = """title: retain browser failure
workflow_definition:
  parameters: []
  blocks:
    - block_type: code
      label: collect_failure_rate
      copilot_block_association: cba_collect_failure_rate
      code: |
        value = await page.locator("canvas.failure-rate").inner_text()
        return {"failure_rate": value}
"""
    ctx = _locator_packet_ctx()
    ctx.workflow_yaml = failed_workflow
    ctx.persisted_workflow_yaml = failed_workflow
    failed_operation = BuildTestFailedOperation(
        kind="browser_operation_failed",
        workflow_run_id="wr_failed",
        block_label="collect_failure_rate",
        failing_line=1,
    )
    record_build_test_outcome(
        ctx,
        RecordedBuildTestOutcome(
            phase="persisted_block_run",
            verdict="repairable_failure",
            reason_code="runtime_block_failure",
            workflow_run_id="wr_failed",
            structural_failure_identity="browser-operation",
            failed_operation=failed_operation,
        ),
    )

    record_build_test_outcome(
        ctx,
        RecordedBuildTestOutcome(
            phase="persisted_block_run",
            verdict="not_authoritative",
            reason_code="run_completed_unevaluated",
            workflow_run_id="wr_later_completed_run",
            requested_block_labels=["collect_failure_rate"],
            executed_block_labels=["collect_failure_rate"],
            executed_block_associations=("cba_collect_failure_rate",),
            completed_block_associations=("cba_collect_failure_rate",),
        ),
    )

    assert ctx.latest_recorded_build_test_outcome is not None
    assert ctx.latest_recorded_build_test_outcome.failed_operation == failed_operation


def test_browser_operation_failure_is_not_cleared_by_an_untested_persisted_edit() -> None:
    failed_workflow = """title: retain browser failure
workflow_definition:
  parameters: []
  blocks:
    - block_type: code
      label: collect_failure_rate
      code: |
        return {"failure_rate": await page.locator("canvas.failure-rate").inner_text()}
"""
    ctx = _locator_packet_ctx()
    ctx.workflow_yaml = failed_workflow
    ctx.persisted_workflow_yaml = failed_workflow
    failed_operation = BuildTestFailedOperation(
        kind="browser_operation_failed",
        workflow_run_id="wr_failed",
        block_label="collect_failure_rate",
        failing_line=1,
    )
    record_build_test_outcome(
        ctx,
        RecordedBuildTestOutcome(
            phase="persisted_block_run",
            attempted_tool="update_and_run_blocks",
            verdict="repairable_failure",
            reason_code="runtime_block_failure",
            workflow_run_id="wr_failed",
            requested_block_labels=["collect_failure_rate"],
            structural_failure_identity="browser-operation",
            failed_operation=failed_operation,
        ),
    )

    ctx.workflow_yaml = failed_workflow.replace("canvas.failure-rate", "[data-testid='failure-rate']")
    ctx.persisted_workflow_yaml = ctx.workflow_yaml
    record_build_test_outcome(ctx, None)

    assert ctx.latest_recorded_build_test_outcome is not None
    assert ctx.latest_recorded_build_test_outcome.failed_operation == failed_operation


def test_failure_in_the_latest_run_keeps_the_stop_and_names_its_block() -> None:
    ctx = _locator_packet_ctx()
    ctx.runner_code_block_associations_by_label = {"continue_to_payment": "cba_continue_to_payment"}
    failed_operation = BuildTestFailedOperation(
        kind="browser_operation_failed",
        workflow_run_id="wr_latest",
        workflow_run_block_id="wrb_latest",
        block_label="continue_to_payment",
        failing_line=3,
        block_association="cba_continue_to_payment",
    )
    record_build_test_outcome(
        ctx,
        RecordedBuildTestOutcome(
            phase="persisted_block_run",
            attempted_tool="update_and_run_blocks",
            verdict="repairable_failure",
            reason_code="runtime_block_failure",
            workflow_run_id="wr_latest",
            attempted_block_label="continue_to_payment",
            block_labels=["search_tomorrow_trip", "continue_to_payment"],
            structural_failure_identity="browser-operation",
            failed_operation=failed_operation,
        ),
    )
    record_build_test_outcome(
        ctx,
        RecordedBuildTestOutcome(
            phase="persisted_block_run",
            attempted_tool="update_and_run_blocks",
            verdict="not_authoritative",
            reason_code="blocker_reported",
            workflow_run_id="wr_latest",
            block_labels=["search_tomorrow_trip", "continue_to_payment"],
        ),
    )

    latest = ctx.latest_recorded_build_test_outcome
    assert latest is not None
    assert latest.failed_operation == failed_operation
    assert latest.completed_block_associations == ()

    envelope = assemble_terminal_envelope(
        response_type="REPLY",
        verified=False,
        workflow_applied=False,
        proposal_disposition="review_untested",
        run_outcomes=[],
        blocker_reason=None,
        halt_kind=None,
        attempted=None,
        workflow_mutated=True,
        workflow_attempted=True,
        failed_operation=latest.failed_operation,
        proposal_present=True,
        interruption=None,
    )
    assert envelope is not None
    assert envelope.terminal_cause == "browser_operation_failed"
    message, replaced = render_terminal_message(envelope, "End-to-end test did not complete.", cancelled=False)

    assert replaced is True
    assert message.startswith(
        "I stopped after a browser operation failed in `continue_to_payment` while testing the workflow."
    )


def test_stop_clears_when_the_failed_block_completed_even_though_a_later_block_failed() -> None:
    ctx = _locator_packet_ctx()
    ctx.runner_code_block_associations_by_label = {"log_in": "cba_log_in", "collect_titles": "cba_collect_titles"}
    record_build_test_outcome(
        ctx,
        RecordedBuildTestOutcome(
            phase="persisted_block_run",
            attempted_tool="update_and_run_blocks",
            verdict="repairable_failure",
            reason_code="runtime_block_failure",
            workflow_run_id="wr_login_failed",
            block_labels=["log_in", "collect_titles"],
            structural_failure_identity="browser-operation",
            failed_operation=BuildTestFailedOperation(
                kind="browser_operation_failed",
                workflow_run_id="wr_login_failed",
                block_label="log_in",
                failing_line=2,
                block_association="cba_log_in",
            ),
        ),
    )
    record_build_test_outcome(
        ctx,
        RecordedBuildTestOutcome(
            phase="persisted_block_run",
            attempted_tool="update_and_run_blocks",
            verdict="repairable_failure",
            reason_code="runtime_block_failure",
            workflow_run_id="wr_collect_failed",
            block_labels=["log_in", "collect_titles"],
            executed_block_labels=["log_in", "collect_titles"],
            executed_block_associations=("cba_log_in", "cba_collect_titles"),
            completed_block_associations=("cba_log_in",),
        ),
    )

    assert ctx.latest_recorded_build_test_outcome is not None
    assert ctx.latest_recorded_build_test_outcome.failed_operation is None


def test_post_run_enrichment_does_not_resurrect_a_released_stop() -> None:
    ctx = _locator_packet_ctx()
    ctx.runner_code_block_associations_by_label = {"log_in": "cba_log_in"}
    record_build_test_outcome(
        ctx,
        RecordedBuildTestOutcome(
            phase="persisted_block_run",
            attempted_tool="update_and_run_blocks",
            verdict="repairable_failure",
            reason_code="runtime_block_failure",
            workflow_run_id="wr_login_failed",
            block_labels=["log_in"],
            structural_failure_identity="browser-operation",
            failed_operation=BuildTestFailedOperation(
                kind="browser_operation_failed",
                workflow_run_id="wr_login_failed",
                block_label="log_in",
                failing_line=2,
                block_association="cba_log_in",
            ),
        ),
    )
    released = RecordedBuildTestOutcome(
        phase="persisted_block_run",
        attempted_tool="update_and_run_blocks",
        verdict="not_authoritative",
        reason_code="run_completed_unevaluated",
        workflow_run_id="wr_login_completed",
        block_labels=["log_in"],
        executed_block_labels=["log_in"],
        executed_block_associations=("cba_log_in",),
        completed_block_associations=("cba_log_in",),
    )
    record_build_test_outcome(ctx, released)
    assert ctx.latest_recorded_build_test_outcome is not None
    assert ctx.latest_recorded_build_test_outcome.failed_operation is None

    assert bind_post_run_page_evidence(
        ctx,
        {"workflow_run_id": "wr_login_completed"},
        None,
        regraded=released,
    )

    assert ctx.latest_recorded_build_test_outcome is not None
    assert ctx.latest_recorded_build_test_outcome.failed_operation is None


def test_stop_holds_when_the_failed_block_only_terminated_in_the_later_run() -> None:
    ctx = _locator_packet_ctx()
    ctx.runner_code_block_associations_by_label = {"log_in": "cba_log_in"}
    failed_operation = BuildTestFailedOperation(
        kind="browser_operation_failed",
        workflow_run_id="wr_login_failed",
        block_label="log_in",
        failing_line=2,
        block_association="cba_log_in",
    )
    record_build_test_outcome(
        ctx,
        RecordedBuildTestOutcome(
            phase="persisted_block_run",
            attempted_tool="update_and_run_blocks",
            verdict="repairable_failure",
            reason_code="runtime_block_failure",
            workflow_run_id="wr_login_failed",
            block_labels=["log_in"],
            structural_failure_identity="browser-operation",
            failed_operation=failed_operation,
        ),
    )
    record_build_test_outcome(
        ctx,
        RecordedBuildTestOutcome(
            phase="persisted_block_run",
            attempted_tool="update_and_run_blocks",
            verdict="not_authoritative",
            reason_code="terminal_challenge_blocker",
            workflow_run_id="wr_login_terminated",
            block_labels=["log_in"],
            executed_block_labels=["log_in"],
            executed_block_associations=("cba_log_in",),
        ),
    )

    assert ctx.latest_recorded_build_test_outcome is not None
    assert ctx.latest_recorded_build_test_outcome.failed_operation == failed_operation


@pytest.mark.asyncio
async def test_browser_operation_outcome_clears_after_server_composed_verified_retest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failed_workflow = """title: read the failure rate
workflow_definition:
  parameters: []
  blocks:
    - block_type: code
      label: collect_failure_rate
      copilot_block_association: cba_collect_failure_rate
      code: |
        value = await page.locator("canvas.failure-rate").inner_text()
        return {"failure_rate": value}
"""
    repaired_workflow = failed_workflow.replace(
        'page.locator("canvas.failure-rate")',
        "page.locator(\"[data-testid='failure-rate']\")",
    )
    ctx = _locator_packet_ctx()
    ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
    ctx.workflow_yaml = failed_workflow
    ctx.persisted_workflow_yaml = failed_workflow
    ctx.runner_code_block_associations_by_label = {"collect_failure_rate": "cba_collect_failure_rate"}
    record_build_test_outcome(
        ctx,
        RecordedBuildTestOutcome(
            phase="persisted_block_run",
            attempted_tool="update_and_run_blocks",
            verdict="repairable_failure",
            reason_code="runtime_block_failure",
            workflow_run_id="wr_browser_operation",
            block_labels=["collect_failure_rate"],
            structural_failure_identity="browser-operation",
            failed_operation=BuildTestFailedOperation(
                kind="browser_operation_failed",
                workflow_run_id="wr_browser_operation",
                workflow_run_block_id="wrb_browser_operation",
                block_label="collect_failure_rate",
                failing_line=1,
                block_association="cba_collect_failure_rate",
            ),
        ),
    )

    prompt = _recorded_build_test_outcome_prompt(ctx)
    persisted_attempts = [failed_workflow]
    tested_attempts: list[str] = []

    async def persist_workflow(
        payload: dict[str, object], copilot_ctx: CopilotContext, **_kwargs: object
    ) -> dict[str, object]:
        candidate = payload["workflow_yaml"]
        assert isinstance(candidate, str)
        assert payload["_preserve_code_block_associations"] is True
        persisted_attempts.append(candidate)
        copilot_ctx.workflow_yaml = candidate
        copilot_ctx.persisted_workflow_yaml = candidate
        code = candidate.split("code: |", 1)[1]
        workflow = SimpleNamespace(
            proxy_location=None,
            workflow_definition=SimpleNamespace(
                blocks=[
                    SimpleNamespace(
                        label="collect_failure_rate",
                        block_type=SimpleNamespace(value="code"),
                        code=code,
                    )
                ]
            ),
        )
        return {"ok": True, "data": {"block_count": 1}, "_workflow": workflow}

    async def test_persisted_workflow(
        _params: dict[str, object], copilot_ctx: CopilotContext, **_kwargs: object
    ) -> dict[str, object]:
        persisted = copilot_ctx.persisted_workflow_yaml
        assert isinstance(persisted, str)
        tested_attempts.append(persisted)
        repaired_selector_present = "[data-testid='failure-rate']" in persisted
        return {
            "ok": repaired_selector_present,
            "data": {
                "workflow_run_id": "wr_retested_repair",
                "overall_status": "completed" if repaired_selector_present else "failed",
                "requested_block_labels": ["collect_failure_rate"],
                "executed_block_labels": ["collect_failure_rate"],
                "blocks": [
                    {
                        "label": "collect_failure_rate",
                        "status": "completed" if repaired_selector_present else "failed",
                        "extracted_data": {"failure_rate": "16.67%"} if repaired_selector_present else None,
                    }
                ],
            },
        }

    async def observe_test_result(
        copilot_ctx: CopilotContext, result: dict[str, object], _handler_start: float
    ) -> object:
        record_build_test_outcome(
            copilot_ctx,
            RecordedBuildTestOutcome(
                phase="persisted_block_run",
                attempted_tool="edit_block_and_run",
                verdict="progress_observed",
                reason_code="verified_success",
                workflow_run_id="wr_retested_repair",
                executed_block_associations=("cba_collect_failure_rate",),
                completed_block_associations=("cba_collect_failure_rate",),
                verified_progress_marker="run-completed",
            ),
        )
        return copilot_ctx.latest_recorded_build_test_outcome

    monkeypatch.setattr(tools_module, "_update_and_run_requires_skipped_run", lambda *args: False)
    monkeypatch.setattr(tools_module, "_authority_tool_error", lambda *args, **kwargs: None)
    monkeypatch.setattr(tools_module, "_get_prior_workflow_definition", AsyncMock(return_value=None))
    monkeypatch.setattr(tools_module, "_frontier_runtime_page_url", AsyncMock(return_value=None))
    monkeypatch.setattr(
        tools_module,
        "_plan_frontier",
        lambda *args: (["collect_failure_rate"], {}, "collect_failure_rate", "initial"),
    )
    monkeypatch.setattr(tools_module, "_update_workflow", persist_workflow)
    monkeypatch.setattr(tools_module, "_run_blocks_and_collect_debug", test_persisted_workflow)
    monkeypatch.setattr(tools_module, "_verify_and_record_run_blocks_result", observe_test_result)
    monkeypatch.setattr(tools_module, "_record_diagnosis_repair_contract", lambda *args, **kwargs: None)
    monkeypatch.setattr(tools_module, "enqueue_screenshot_from_result", lambda *args, **kwargs: None)

    tool_result = await tools_module.edit_block_and_run_tool.on_invoke_tool(
        SimpleNamespace(context=ctx, tool_name="edit_block_and_run"),
        json.dumps(
            {
                "label": "collect_failure_rate",
                "expected_code": 'value = await page.locator("canvas.failure-rate").inner_text()\nreturn {"failure_rate": value}',
                "replacement_code": (
                    "value = await page.locator(\"[data-testid='failure-rate']\").inner_text()\n"
                    'return {"failure_rate": value}'
                ),
                "block_labels": ["collect_failure_rate"],
                "parameters": {},
            }
        ),
    )
    parsed_result = json.loads(tool_result)

    assert "browser_operation_failed" in prompt
    assert "wrb_browser_operation" in prompt
    assert "collect_failure_rate" in prompt
    assert "failing_line=1" in prompt
    assert persisted_attempts == [failed_workflow, repaired_workflow]
    assert "canvas.failure-rate" in persisted_attempts[0]
    assert "canvas.failure-rate" not in persisted_attempts[1]
    assert tested_attempts == [repaired_workflow]
    assert parsed_result["ok"] is True
    assert parsed_result["data"]["workflow_run_id"] == "wr_retested_repair"
    assert parsed_result["data"]["build_test_packet"]["run"] == {
        "workflow_run_id": "wr_retested_repair",
        "status": "completed",
    }
    assert ctx.latest_recorded_build_test_outcome is not None
    assert ctx.latest_recorded_build_test_outcome.workflow_run_id == "wr_retested_repair"
    assert ctx.latest_recorded_build_test_outcome.failed_operation is None


def test_browser_operation_identity_is_bounded_by_the_shared_packet_projection() -> None:
    long_identity = "x" * 200
    result = _failed_run_result(None)
    data = result["data"]
    assert isinstance(data, dict)
    data["workflow_run_id"] = long_identity
    data["blocks"] = [
        {
            "workflow_run_block_id": long_identity,
            "label": long_identity,
            "status": "failed",
            "error_codes": ["browser_operation_failed"],
        }
    ]

    projected = project_build_test_packet_for_llm(build_test_evidence_packet(_locator_packet_ctx(), result))

    assert projected.failure is not None
    assert projected.failure.failed_operation is not None
    operation = projected.failure.failed_operation
    assert len(operation.workflow_run_id or "") == 160
    assert len(operation.workflow_run_block_id or "") == 160
    assert len(operation.block_label or "") == 160
    assert all(
        value.endswith("...")
        for value in (operation.workflow_run_id, operation.workflow_run_block_id, operation.block_label)
        if value
    )
    assert any("failure.failed_operation.workflow_run_id shortened" in notice for notice in projected.omission_notices)
    assert any(
        "failure.failed_operation.workflow_run_block_id shortened" in notice for notice in projected.omission_notices
    )
    assert any("failure.failed_operation.block_label shortened" in notice for notice in projected.omission_notices)


def test_connect_failure_identity_is_bounded_by_the_shared_packet_projection() -> None:
    long_identity = "x" * 200
    result = {
        "ok": False,
        "data": {
            "overall_status": "setup_failed",
            "blocks": [],
            "build_test_connect_failure": BuildTestConnectFailure(
                state="cdp_connect_failed",
                workflow_run_id=long_identity,
                workflow_run_block_id=long_identity,
                task_id=long_identity,
                browser_session_id=long_identity,
            ).model_dump(mode="json"),
        },
    }

    projected = project_build_test_packet_for_llm(build_test_evidence_packet(_locator_packet_ctx(), result))

    assert projected.failure is not None
    assert projected.failure.connect_failure is not None
    failure = projected.failure.connect_failure
    identities = (
        failure.workflow_run_id,
        failure.workflow_run_block_id,
        failure.task_id,
        failure.browser_session_id,
    )
    assert all(len(identity or "") == 160 for identity in identities)
    assert all((identity or "").endswith("...") for identity in identities)
    assert sum("failure.connect_failure" in notice for notice in projected.omission_notices) == 4


def test_browser_operation_packet_names_each_unavailable_operation_identity() -> None:
    result = _failed_run_result(None)
    data = result["data"]
    assert isinstance(data, dict)
    data.pop("workflow_run_id", None)
    data["blocks"] = [
        {
            "label": None,
            "status": "failed",
            "error_codes": ["browser_operation_failed"],
        }
    ]
    data.pop("failing_code_line", None)

    packet = build_test_evidence_packet(_locator_packet_ctx(), result)

    assert packet.failure is not None
    assert packet.failure.failed_operation is not None
    notices = packet.omission_notices
    assert any("failure.failed_operation.workflow_run_id omitted" in notice for notice in notices)
    assert any("failure.failed_operation.workflow_run_block_id omitted" in notice for notice in notices)
    assert any("failure.failed_operation.block_label omitted" in notice for notice in notices)
    assert any("failure.failed_operation.failing_line omitted" in notice for notice in notices)


def test_a_failure_the_runner_did_not_type_carries_neither_field() -> None:
    packet = build_test_evidence_packet(_locator_packet_ctx(), _failed_run_result(None))

    assert packet.failure is not None
    assert packet.failure.error_codes == []
    assert packet.failure.failing_line is None


def test_native_failed_block_identity_survives_packet_projection_and_aggregate_compaction() -> None:
    result = _failed_run_result(None)
    data = result["data"]
    assert isinstance(data, dict)
    data["action_observations"] = ["click completed"]
    data["blocks"] = [
        {
            "workflow_run_block_id": "wrb_native",
            "task_id": "tsk_native",
            "step_id": "stp_native",
            "label": "native_task",
            "block_type": "TASK",
            "status": "failed",
            "failure_reason": "Reached the maximum steps (30)",
        }
    ]

    outcome = recorded_outcome_from_run_blocks_result(result)
    packet = build_test_evidence_packet(_locator_packet_ctx(), result, recorded_outcome=outcome)
    projected = project_build_test_packet_for_llm(packet)
    compacted = project_build_test_packet_for_llm(_oversized_packet(packet))

    assert packet.failure is not None
    assert projected.failure is not None
    assert compacted.failure is not None
    expected = ("wrb_native", "tsk_native", "stp_native", "TASK")
    for failure in (packet.failure, projected.failure, compacted.failure):
        assert (
            failure.workflow_run_block_id,
            failure.task_id,
            failure.step_id,
            failure.block_type,
        ) == expected
    assert [notice for notice in projected.omission_notices if notice.startswith("failure.page_state omitted:")] == [
        "failure.page_state omitted: no bounded same-run page state was recorded."
    ]


def test_historical_failure_packet_without_native_identities_remains_valid() -> None:
    failure = BuildTestPacketFailure(block_label="code", block_status="failed", reason="boom")

    assert failure.workflow_run_block_id is None
    assert failure.task_id is None
    assert failure.step_id is None
    assert failure.block_type is None


def test_recorded_run_block_result_keeps_native_machine_identities() -> None:
    row = SimpleNamespace(
        workflow_run_block_id="wrb_native",
        task_id="tsk_native",
        label="native_task",
        block_type=SimpleNamespace(name="TASK"),
        status="failed",
        failure_reason="Reached the maximum steps (30)",
        error_codes=["max_steps_exceeded"],
        output=None,
    )

    result = _recorded_run_block_result(row)

    assert result == {
        "workflow_run_block_id": "wrb_native",
        "task_id": "tsk_native",
        "label": "native_task",
        "block_type": "TASK",
        "status": "failed",
        "failure_reason": "Reached the maximum steps (30)",
        "error_codes": ["max_steps_exceeded"],
        "output": None,
    }


@pytest.fixture
def synthetic_native_actions_newest_first() -> list[SimpleNamespace]:
    """Synthetic rows exercise ordering and privacy without claiming production custody."""
    return [
        SimpleNamespace(
            task_id="tsk_synthetic",
            step_id="stp_newest",
            action_type="click",
            status="completed",
            reasoning="private reasoning newest",
            element_id="private-element-newest",
            output=None,
            response=None,
        ),
        *[
            SimpleNamespace(
                task_id="tsk_synthetic",
                step_id=f"stp_{number}",
                action_type=action_type,
                status="completed",
                reasoning=f"private reasoning {number}",
                element_id=f"private-element-{number}",
                output=None,
                response=None,
            )
            for number, action_type in [
                (5, "scroll"),
                (4, "wait"),
                (3, "hover"),
                (2, "select_option"),
                (1, "input_text"),
                (0, "goto_url"),
            ]
        ],
    ]


@pytest.mark.asyncio
async def test_native_actions_use_newest_step_and_keep_newest_six_chronological(
    monkeypatch: pytest.MonkeyPatch,
    synthetic_native_actions_newest_first: list[SimpleNamespace],
) -> None:
    block = SimpleNamespace(task_id="tsk_synthetic")
    result = {"status": "failed"}
    fake_app = SimpleNamespace(
        DATABASE=SimpleNamespace(
            tasks=SimpleNamespace(
                get_recent_actions_for_tasks=AsyncMock(return_value=synthetic_native_actions_newest_first)
            )
        )
    )
    monkeypatch.setattr(run_execution_module, "app", fake_app)

    await run_execution_module._attach_action_traces([block], [result], "org_native")

    assert result["step_id"] == "stp_newest"
    assert run_execution_module._retained_action_observations([result]) == [
        "input_text completed",
        "select_option completed",
        "hover completed",
        "wait completed",
        "scroll completed",
        "click completed",
    ]
    summary = run_execution_module._failure_action_trace_summary(result)
    assert summary == run_execution_module._retained_action_observations([result])
    assert "private" not in str(summary)
    assert "goto_url" not in str(summary)


def test_native_actions_bound_the_global_newest_slice_before_chronological_rendering() -> None:
    results = [
        {
            "action_trace": [
                {"action": "click", "status": "completed"},
                {"action": "scroll", "status": "completed"},
                {"action": "wait", "status": "completed"},
                {"action": "hover", "status": "completed"},
            ]
        },
        {
            "action_trace": [
                {"action": "select_option", "status": "completed"},
                {"action": "input_text", "status": "completed"},
                {"action": "goto_url", "status": "completed"},
                {"action": "reload_page", "status": "completed"},
            ]
        },
    ]

    assert run_execution_module._retained_action_observations(results) == [
        "input_text completed",
        "select_option completed",
        "hover completed",
        "wait completed",
        "scroll completed",
        "click completed",
    ]


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

    # `run_workflow_end_to_end` is the third run surface and builds its own hand-back.
    end_to_end = _Path(__file__).resolve().parents[2] / "skyvern/forge/sdk/copilot/tools/run_execution.py"
    end_to_end_source = end_to_end.read_text()
    body_start = end_to_end_source.index("async def run_workflow_end_to_end(")
    body = end_to_end_source[body_start : end_to_end_source.index("async def _run_blocks_and_collect_debug(")]
    assert "_carry_prior_attempt_change_identity_into_result(" in body


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


_REPEAT_FAILURE_REASON = "CodeBlock failed with NameError at line 1: name 'passenger_count' is not defined."
_REPEAT_RAISING_CODE = "total = passenger_count * fare\n"
_REPEAT_CHANGED_CODE = "total = int(await page.locator('#pax').input_value()) * fare\n"


def _record_failing_attempt(ctx: SimpleNamespace, code: str, workflow_run_id: str) -> dict:
    """Run one failing attempt of the same block against `code`, returning that run's own result.
    The failing block registers a download, because a failing hand-back is not a minimal payload."""
    ctx.workflow_yaml = _raising_code_workflow_yaml(code)
    ctx.persisted_workflow_yaml = ctx.workflow_yaml
    result = _generated_code_exception_result(
        workflow_run_id,
        _REPEAT_FAILURE_REASON,
        downloaded_file_artifact_ids=[f"a_dl_{workflow_run_id}"],
    )
    record_build_test_outcome(
        ctx,
        recorded_outcome_from_run_blocks_result(
            result,
            recorded_run_outcome=RecordedRunOutcome(
                verdict="not_demonstrated",
                reason_code="blocker_reported",
                display_reason=_REPEAT_FAILURE_REASON,
                workflow_run_id=workflow_run_id,
                run_completed=False,
            ),
        ),
    )
    return result


def test_a_repeat_failing_run_reports_whether_the_code_it_ran_changed() -> None:
    """Nothing else answers it: the rendered source binding compares the latest attempt to the current
    definition, which on a repeat failure reads as a match and says nothing about the prior attempt."""
    ctx = _run_history_ctx(_raising_code_workflow_yaml(_REPEAT_RAISING_CODE))

    first = _record_failing_attempt(ctx, _REPEAT_RAISING_CODE, "wr_1")
    _carry_unresolved_failure_into_result(ctx, first, "edit_block_and_run")
    assert "prior_attempt_change_identity" not in first["data"]

    resubmitted = _record_failing_attempt(ctx, _REPEAT_RAISING_CODE, "wr_2")
    _carry_unresolved_failure_into_result(ctx, resubmitted, "edit_block_and_run")
    assert resubmitted["data"]["prior_attempt_change_identity"] == {
        "prior_workflow_run_id": "wr_1",
        "block_label": "book_trip",
        "changed": False,
        "basis": "code_hash",
    }

    repaired = _record_failing_attempt(ctx, _REPEAT_CHANGED_CODE, "wr_3")
    _carry_unresolved_failure_into_result(ctx, repaired, "edit_block_and_run")
    assert repaired["data"]["prior_attempt_change_identity"] == {
        "prior_workflow_run_id": "wr_2",
        "block_label": "book_trip",
        "changed": True,
        "basis": "code_hash",
    }
    assert repaired["data"]["blocks"][0]["extracted_data"] == {"downloaded_file_artifact_ids": ["a_dl_wr_3"]}


def test_legs_that_re_ran_no_code_carry_no_change_identity() -> None:
    """The validation and update legs return before any run, and a passing run is the other seam's
    question; a comparison reported on either would stand behind no execution."""
    ctx = _run_history_ctx(_raising_code_workflow_yaml(_REPEAT_RAISING_CODE))
    _record_failing_attempt(ctx, _REPEAT_RAISING_CODE, "wr_1")
    _record_failing_attempt(ctx, _REPEAT_RAISING_CODE, "wr_2")

    rejected = {"ok": False, "error": "block_labels must include the edited block 'book_trip'."}
    _carry_unresolved_failure_into_result(ctx, rejected, "edit_block_and_run")
    assert rejected == {"ok": False, "error": "block_labels must include the edited block 'book_trip'."}

    passing = {"ok": True, "data": {"workflow_run_id": "wr_3", "overall_status": "completed"}}
    _carry_unresolved_failure_into_result(ctx, passing, "edit_block_and_run")
    assert "prior_attempt_change_identity" not in passing["data"]


def test_the_change_identity_survives_sanitization_and_compaction() -> None:
    """The model may reach the repair several turns later, after the output has been compacted."""
    ctx = _run_history_ctx(_raising_code_workflow_yaml(_REPEAT_RAISING_CODE))
    _record_failing_attempt(ctx, _REPEAT_RAISING_CODE, "wr_1")
    resubmitted = _record_failing_attempt(ctx, _REPEAT_RAISING_CODE, "wr_2")
    _carry_unresolved_failure_into_result(ctx, resubmitted, "edit_block_and_run")
    carried = resubmitted["data"]["prior_attempt_change_identity"]

    sanitized = sanitize_tool_result_for_llm("run_blocks_and_collect_debug", resubmitted)
    assert sanitized["data"]["prior_attempt_change_identity"] == carried

    compacted = json.loads(_summarize_tool_output(json.dumps(resubmitted)))
    assert compacted["prior_attempt_change_identity"] == carried


def test_an_attempt_recorded_before_this_field_existed_reports_unavailable_not_unchanged() -> None:
    """A turn already in flight when this ships has a prior entry with no code hash, and reporting
    that silence as `changed: False` would accuse the model of resubmitting code nobody compared."""
    ctx = _run_history_ctx(_raising_code_workflow_yaml(_REPEAT_RAISING_CODE))
    _record_failing_attempt(ctx, _REPEAT_RAISING_CODE, "wr_1")
    ctx.recorded_build_test_outcome_history[0].pop("attempted_block_code_hash")

    resubmitted = _record_failing_attempt(ctx, _REPEAT_RAISING_CODE, "wr_2")
    _carry_unresolved_failure_into_result(ctx, resubmitted, "edit_block_and_run")

    assert resubmitted["data"]["prior_attempt_change_identity"] == {
        "prior_workflow_run_id": "wr_1",
        "block_label": "book_trip",
        "changed": None,
        "basis": "unavailable",
    }


def _completed_output_run_result(retained_value: object, *, register_row: bool = True) -> dict[str, object]:
    run_id = "wr_requested_output"
    registered_row = {
        "workflow_run_id": run_id,
        "output_parameter_id": "op_payment_options",
        "output_parameter_key": "collect_options_output",
        "block_label": "collect_options",
        "block_type": "code",
        "value": retained_value,
    }
    return {
        "ok": True,
        "data": {
            "workflow_run_id": run_id,
            "overall_status": "completed",
            "requested_block_labels": ["collect_options"],
            "executed_block_labels": ["collect_options"],
            "blocks": [
                {
                    "label": "collect_options",
                    "block_type": "code",
                    "status": "completed",
                    "output": retained_value,
                    "extracted_data": {"collect_options_output": retained_value},
                }
            ],
            "requested_output_parameter_definitions": [
                {
                    "workflow_run_id": run_id,
                    "output_parameter_id": "op_payment_options",
                    "output_parameter_key": "collect_options_output",
                    "block_label": "collect_options",
                    "block_type": "code",
                }
            ],
            "registered_output_parameter_values": [registered_row] if register_row else [],
        },
    }


@pytest.mark.parametrize(
    ("case", "retained_value", "register_row", "expected_reason_code"),
    [
        ("no_row", {"payment_options": ["Visa"]}, False, "registered_output_missing"),
        ("null_value", None, True, "registered_output_null"),
        # An empty collection is a value the code returned. Whether "no options were offered" is
        # the right answer is the model's call; reporting it absent would invite invented data.
        ("empty_object", {}, True, None),
        ("empty_list", [], True, None),
        ("run_owned_value", {"payment_options": ["Visa", "PayPal"]}, True, None),
    ],
)
def test_a_completed_run_that_retained_no_requested_output_value_returns_to_ordinary_repair(
    case: str,
    retained_value: object,
    register_row: bool,
    expected_reason_code: str | None,
) -> None:
    """A retained row proves the block ran; it is not proof the requested output was produced."""
    result = _completed_output_run_result(retained_value, register_row=register_row)
    data = result["data"]
    assert isinstance(data, dict)

    outcome = recorded_outcome_from_run_blocks_result(
        result,
        recorded_run_outcome=RecordedRunOutcome(
            verdict="not_evaluated",
            workflow_run_id="wr_requested_output",
            run_completed=True,
        ),
        registered_output_parameter_payloads=data["registered_output_parameter_values"],
    )
    assert outcome is not None
    packet = project_build_test_packet_for_llm(
        build_test_evidence_packet(_locator_packet_ctx(), result, recorded_outcome=outcome)
    ).model_dump(mode="json", exclude_none=True)
    ordinary_input = _build_user_context(
        workflow_yaml="",
        chat_history_text="",
        global_llm_context="",
        debug_run_info_text=_prior_run_debug_text(packet),
        user_message="Repair the recorded run.",
    )

    if expected_reason_code is None:
        assert outcome.missing_requested_output_facts == []
        assert outcome.verdict == "not_authoritative"
        assert packet["unfinished_items"] == []
        if case == "run_owned_value":
            assert '"payment_options"' in ordinary_input, "the run-owned value is not handed back"
        return

    assert outcome.verdict == "repairable_failure"
    assert outcome.reason_code == "no_meaningful_output"
    assert outcome.is_authoritative is True
    assert [fact["reason_code"] for fact in outcome.missing_requested_output_facts] == [expected_reason_code]
    assert [fact["output_path"] for fact in outcome.missing_requested_output_facts] == ["output.collect_options_output"]
    assert {
        "kind": "missing_requested_output",
        "label": "collect_options",
        "output_path": "output.collect_options_output",
        "reason_code": expected_reason_code,
    } in packet["unfinished_items"]
    assert '"output_path": "output.collect_options_output"' in ordinary_input
    assert f'"reason_code": "{expected_reason_code}"' in ordinary_input


def test_snapshot_registered_row_with_regenerated_id_satisfies_requested_output_by_key() -> None:
    result = _completed_output_run_result({"payment_options": ["Visa"]})
    data = result["data"]
    assert isinstance(data, dict)
    registered = data["registered_output_parameter_values"]
    assert isinstance(registered, list)
    registered[0]["output_parameter_id"] = "op_snapshot_regenerated"

    outcome = recorded_outcome_from_run_blocks_result(
        result,
        recorded_run_outcome=RecordedRunOutcome(
            verdict="not_evaluated",
            workflow_run_id="wr_requested_output",
            run_completed=True,
        ),
        registered_output_parameter_payloads=registered,
    )

    assert outcome is not None
    assert outcome.missing_requested_output_facts == []
    assert outcome.verdict == "not_authoritative"


def test_row_only_registered_loop_output_reaches_the_model_visible_packet() -> None:
    loop_value = [[{"record_number": "123"}], [{"record_number": "456"}]]
    result = _completed_output_run_result(loop_value)
    data = result["data"]
    assert isinstance(data, dict)
    blocks = data["blocks"]
    assert isinstance(blocks, list)
    blocks[0].pop("output")
    blocks[0].pop("extracted_data")

    packet = project_build_test_packet_for_llm(build_test_evidence_packet(_locator_packet_ctx(), result)).model_dump(
        mode="json", exclude_none=True
    )
    ordinary_input = _build_user_context(
        workflow_yaml="",
        chat_history_text="",
        global_llm_context="",
        debug_run_info_text=_prior_run_debug_text(packet),
        user_message="Repair the recorded run.",
    )

    assert packet["registered_outputs"] == [
        {
            "label": "collect_options",
            "status": "completed",
            "output": {"collect_options_output": loop_value},
            "value_complete": True,
        }
    ]
    assert "123" in ordinary_input
    assert "456" in ordinary_input
    assert any("output-parameter rows" in notice for notice in packet["omission_notices"])


def test_partial_loop_fallback_precedes_twelve_recorded_outputs_and_has_no_fabricated_status() -> None:
    loop_value = [[{"record_number": "123"}]]
    result = _completed_output_run_result(loop_value)
    data = result["data"]
    assert isinstance(data, dict)
    data["blocks"] = [
        {"label": f"inner_{index}", "status": "completed", "output": {"index": index}} for index in range(12)
    ]

    packet = project_build_test_packet_for_llm(build_test_evidence_packet(_locator_packet_ctx(), result)).model_dump(
        mode="json", exclude_none=True
    )

    assert len(packet["registered_outputs"]) == 12
    assert packet["registered_outputs"][0] == {
        "label": "collect_options",
        "output": {"collect_options_output": loop_value},
        "value_complete": True,
    }
    assert any("registered_outputs shortened" in notice for notice in packet["omission_notices"])


def test_explicit_null_block_output_rejects_stale_registered_row_fallback() -> None:
    result = _completed_output_run_result({"stale": True})
    data = result["data"]
    assert isinstance(data, dict)
    blocks = data["blocks"]
    assert isinstance(blocks, list)
    blocks[0]["output"] = None

    packet = build_test_evidence_packet(_locator_packet_ctx(), result).model_dump(mode="json", exclude_none=True)

    assert packet["registered_outputs"] == []
    assert any("explicit null workflow run block output" in notice for notice in packet["omission_notices"])


def test_a_declared_goal_path_the_run_left_empty_reaches_repair_and_the_latch_from_one_source() -> None:
    """The terminal latch and ordinary repair read the same unmet declared goal paths."""
    run_id = "wr_goal_paths"
    ctx = _locator_packet_ctx()
    ctx.code_artifact_metadata = {
        "collect_options": {
            "claimed_outcomes": [
                {"id": "options", "goal_value_paths": ["$.payment_options", "$.cart_line_item"]},
            ]
        }
    }
    retained_value = {"url": "https://example.test/cart", "actions": ["click", "click"], "payment_options": ["Visa"]}
    result: dict[str, object] = {
        "ok": True,
        "data": {
            "workflow_run_id": run_id,
            "overall_status": "completed",
            "requested_block_labels": ["collect_options"],
            "executed_block_labels": ["collect_options"],
            "blocks": [
                {
                    "label": "collect_options",
                    "block_type": "code",
                    "status": "completed",
                    "extracted_data": retained_value,
                }
            ],
        },
    }

    _anti_bot, empty_data_blocks, _categories, goal_path_omissions = run_execution_module._analyze_run_blocks(
        result, ctx
    )
    outcome = recorded_outcome_from_run_blocks_result(
        result,
        recorded_run_outcome=RecordedRunOutcome(verdict="not_evaluated", workflow_run_id=run_id, run_completed=True),
        declared_goal_path_omissions=goal_path_omissions,
    )
    assert outcome is not None
    packet = project_build_test_packet_for_llm(
        build_test_evidence_packet(ctx, result, recorded_outcome=outcome)
    ).model_dump(mode="json", exclude_none=True)

    # The latch already refused this run; the same unmet paths now reach repair by name.
    assert empty_data_blocks is True
    assert goal_path_omissions == [{"block_label": "collect_options", "output_path": "cart_line_item"}]
    assert outcome.verdict == "repairable_failure"
    assert outcome.reason_code == "no_meaningful_output"
    assert outcome.missing_requested_output_facts == [
        {
            "output_path": "cart_line_item",
            "output_root": "cart_line_item",
            "reason_code": "declared_goal_path_absent",
            "value_status": "no_typed_value",
            "block_label": "collect_options",
        }
    ]
    assert {
        "kind": "missing_requested_output",
        "label": "collect_options",
        "output_path": "cart_line_item",
        "reason_code": "declared_goal_path_absent",
    } in packet["unfinished_items"]

    satisfied_result = json.loads(json.dumps(result))
    satisfied_result["data"]["blocks"][0]["extracted_data"]["cart_line_item"] = {"qty": 1}
    _anti_bot, satisfied_empty, _categories, satisfied_omissions = run_execution_module._analyze_run_blocks(
        satisfied_result, ctx
    )
    assert satisfied_empty is False
    assert satisfied_omissions == []


def test_a_top_level_array_goal_path_is_not_dropped_for_having_no_root() -> None:
    """``$[*].number`` normalizes to ``[].number``, whose root is empty; the path still binds."""
    run_id = "wr_array_goal_path"
    ctx = _locator_packet_ctx()
    ctx.code_artifact_metadata = {
        "collect_rows": {"claimed_outcomes": [{"id": "rows", "goal_value_paths": ["$[*].number"]}]}
    }
    result: dict[str, object] = {
        "ok": True,
        "data": {
            "workflow_run_id": run_id,
            "overall_status": "completed",
            "requested_block_labels": ["collect_rows"],
            "executed_block_labels": ["collect_rows"],
            "blocks": [
                {
                    "label": "collect_rows",
                    "block_type": "code",
                    "status": "completed",
                    "extracted_data": {"url": "https://example.test/rows"},
                }
            ],
        },
    }

    _anti_bot, empty_data_blocks, _categories, goal_path_omissions = run_execution_module._analyze_run_blocks(
        result, ctx
    )
    outcome = recorded_outcome_from_run_blocks_result(
        result,
        recorded_run_outcome=RecordedRunOutcome(verdict="not_evaluated", workflow_run_id=run_id, run_completed=True),
        declared_goal_path_omissions=goal_path_omissions,
    )
    assert outcome is not None

    assert empty_data_blocks is True
    assert outcome.verdict == "repairable_failure"
    assert outcome.missing_requested_output_facts == [
        {
            "output_path": "[].number",
            "reason_code": "declared_goal_path_absent",
            "value_status": "no_typed_value",
            "block_label": "collect_rows",
        }
    ]


def test_two_blocks_omitting_the_same_declared_path_both_reach_repair() -> None:
    """Deduping on the path alone would repair one block and leave the other silently broken."""
    run_id = "wr_shared_goal_path"
    ctx = _locator_packet_ctx()
    ctx.code_artifact_metadata = {
        label: {"claimed_outcomes": [{"id": label, "goal_value_paths": ["$.status"]}]}
        for label in ("collect_first", "collect_second")
    }
    result: dict[str, object] = {
        "ok": True,
        "data": {
            "workflow_run_id": run_id,
            "overall_status": "completed",
            "requested_block_labels": ["collect_first", "collect_second"],
            "executed_block_labels": ["collect_first", "collect_second"],
            "blocks": [
                {
                    "label": label,
                    "block_type": "code",
                    "status": "completed",
                    "extracted_data": {"url": f"https://example.test/{label}"},
                }
                for label in ("collect_first", "collect_second")
            ],
        },
    }

    _anti_bot, _empty, _categories, goal_path_omissions = run_execution_module._analyze_run_blocks(result, ctx)
    outcome = recorded_outcome_from_run_blocks_result(
        result,
        recorded_run_outcome=RecordedRunOutcome(verdict="not_evaluated", workflow_run_id=run_id, run_completed=True),
        declared_goal_path_omissions=goal_path_omissions,
    )
    assert outcome is not None

    assert [(fact["output_path"], fact["block_label"]) for fact in outcome.missing_requested_output_facts] == [
        ("status", "collect_first"),
        ("status", "collect_second"),
    ]

    # Retaining both facts is only half the fix: repair must be able to tell them apart.
    packet = project_build_test_packet_for_llm(
        build_test_evidence_packet(ctx, result, recorded_outcome=outcome)
    ).model_dump(mode="json", exclude_none=True)
    assert [
        (item["output_path"], item["label"])
        for item in packet["unfinished_items"]
        if item["kind"] == "missing_requested_output"
    ] == [("status", "collect_first"), ("status", "collect_second")]

    ctx.latest_recorded_build_test_outcome = outcome
    ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
    rendered = _recorded_build_test_outcome_prompt(ctx)
    assert "block_label=collect_first" in rendered
    assert "block_label=collect_second" in rendered


def test_a_long_declared_goal_path_reaches_repair_uncut() -> None:
    """The prompt tells the model to copy output_path verbatim; a clipped path names nothing."""
    run_id = "wr_long_goal_path"
    long_path = "$.checkout.summary." + ".".join(f"level_{index:02d}" for index in range(15)) + ".amount_due"
    normalized = long_path.removeprefix("$.")
    # Wider than every ceiling the path crosses on its way to the model, and inside the one the
    # facts themselves carry, so a survivor proves the projections and not a shorter fixture.
    assert 160 < len(normalized) <= 180, "the fixture must exceed every downstream bound"

    ctx = _locator_packet_ctx()
    ctx.code_artifact_metadata = {
        "collect_total": {"claimed_outcomes": [{"id": "total", "goal_value_paths": [long_path]}]}
    }
    result: dict[str, object] = {
        "ok": True,
        "data": {
            "workflow_run_id": run_id,
            "overall_status": "completed",
            "requested_block_labels": ["collect_total"],
            "executed_block_labels": ["collect_total"],
            "blocks": [
                {
                    "label": "collect_total",
                    "block_type": "code",
                    "status": "completed",
                    "extracted_data": {"url": "https://example.test/checkout"},
                }
            ],
        },
    }

    _anti_bot, _empty, _categories, goal_path_omissions = run_execution_module._analyze_run_blocks(result, ctx)
    outcome = recorded_outcome_from_run_blocks_result(
        result,
        recorded_run_outcome=RecordedRunOutcome(verdict="not_evaluated", workflow_run_id=run_id, run_completed=True),
        declared_goal_path_omissions=goal_path_omissions,
    )
    assert outcome is not None

    assert [fact["output_path"] for fact in outcome.missing_requested_output_facts] == [normalized]

    packet = project_build_test_packet_for_llm(
        build_test_evidence_packet(ctx, result, recorded_outcome=outcome)
    ).model_dump(mode="json", exclude_none=True)
    assert [
        item["output_path"] for item in packet["unfinished_items"] if item["kind"] == "missing_requested_output"
    ] == [normalized]

    ctx.latest_recorded_build_test_outcome = outcome
    ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
    assert f"output_path={normalized}" in _recorded_build_test_outcome_prompt(ctx)


def _goal_path_run_result(run_id: str, blocks: list[dict[str, object]]) -> dict[str, object]:
    labels = [str(block["label"]) for block in blocks]
    return {
        "ok": True,
        "data": {
            "workflow_run_id": run_id,
            "overall_status": "completed",
            "requested_block_labels": labels,
            "executed_block_labels": labels,
            "blocks": blocks,
        },
    }


def test_a_sibling_blocks_complete_record_does_not_clear_another_blocks_omission() -> None:
    """A run must not be credited for a path one block omitted because a different block proved its own."""
    run_id = "wr_sibling_record"
    ctx = _locator_packet_ctx()
    ctx.code_artifact_metadata = {
        "collect_options": {
            "claimed_outcomes": [{"id": "options", "goal_value_paths": ["$.payment_options", "$.cart_line_item"]}]
        }
    }
    result = _goal_path_run_result(
        run_id,
        [
            {
                "label": "collect_options",
                "block_type": "code",
                "status": "completed",
                "extracted_data": {
                    "url": "https://example.test/cart",
                    "actions": ["click", "click"],
                    "payment_options": ["Visa"],
                },
            },
            {
                "label": "lookup_record",
                "block_type": "code",
                "status": "completed",
                "extracted_data": {
                    "lookup_record_output": {
                        "entity_found": True,
                        "entity_name": "Jordan Example",
                        "record_number": "1234567890",
                        "items": [{"item_label": "Sample Practice", "status": "Active"}],
                        "overall_status": "Active",
                    }
                },
            },
        ],
    )

    _anti_bot, empty_data_blocks, _categories, goal_path_omissions = run_execution_module._analyze_run_blocks(
        result, ctx
    )
    outcome = recorded_outcome_from_run_blocks_result(
        result,
        recorded_run_outcome=RecordedRunOutcome(verdict="not_evaluated", workflow_run_id=run_id, run_completed=True),
        declared_goal_path_omissions=goal_path_omissions,
    )
    assert outcome is not None

    assert goal_path_omissions == [{"block_label": "collect_options", "output_path": "cart_line_item"}]
    assert empty_data_blocks is True
    assert outcome.verdict == "repairable_failure"

    ctx.latest_recorded_build_test_outcome = outcome
    ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
    rendered = _recorded_build_test_outcome_prompt(ctx)
    assert "output_path=cart_line_item" in rendered
    assert "block_label=collect_options" in rendered


def test_a_completed_block_that_retained_nothing_at_all_reports_every_declared_path() -> None:
    """Retaining no output is the strongest omission there is; it must not skip the arm that records it."""
    run_id = "wr_null_extracted"
    ctx = _locator_packet_ctx()
    ctx.code_artifact_metadata = {
        "collect_options": {
            "claimed_outcomes": [{"id": "options", "goal_value_paths": ["$.payment_options", "$.cart_line_item"]}]
        }
    }
    result = _goal_path_run_result(
        run_id,
        [{"label": "collect_options", "block_type": "code", "status": "completed", "extracted_data": None}],
    )

    _anti_bot, empty_data_blocks, _categories, goal_path_omissions = run_execution_module._analyze_run_blocks(
        result, ctx
    )
    outcome = recorded_outcome_from_run_blocks_result(
        result,
        recorded_run_outcome=RecordedRunOutcome(verdict="not_evaluated", workflow_run_id=run_id, run_completed=True),
        declared_goal_path_omissions=goal_path_omissions,
    )
    assert outcome is not None

    assert empty_data_blocks is True
    assert [omission["output_path"] for omission in goal_path_omissions] == ["payment_options", "cart_line_item"]
    assert outcome.verdict == "repairable_failure"

    packet = project_build_test_packet_for_llm(
        build_test_evidence_packet(ctx, result, recorded_outcome=outcome)
    ).model_dump(mode="json", exclude_none=True)
    assert sorted(
        item["output_path"] for item in packet["unfinished_items"] if item["kind"] == "missing_requested_output"
    ) == ["cart_line_item", "payment_options"]


class _CheckoutPage:
    """The focused local fixture the repaired block reads; no browser, no network."""

    _TEXT = {
        "#cart-line-item": "Running Jacket / Size M / Qty 1 / 88.00",
        "#payment-methods": "Card, Wallet, Pay in 4",
        "#selected-product": "Running Jacket",
    }

    async def inner_text(self, selector: str) -> str:
        return self._TEXT[selector]


_UNREPAIRED_CODE = """
url = "https://example.test/checkout"
actions = ["goto", "click", "click"]
"""

_REPAIRED_CODE = """
url = "https://example.test/checkout"
actions = ["goto", "click", "click"]
line_item_text = await page.inner_text("#cart-line-item")
name, size, quantity, price = [part.strip() for part in line_item_text.split("/")]
cart_line_item = {"name": name, "size": size, "quantity": quantity, "price": price}
payment_options = [option.strip() for option in (await page.inner_text("#payment-methods")).split(",")]
selection_output = {"product": await page.inner_text("#selected-product"), "price": price}
"""


async def _execute_code_block(monkeypatch: pytest.MonkeyPatch, code: str) -> dict[str, object]:
    """Run the block through the real CodeBlock executor and return the output it produced."""

    class FakeBrowserState:
        def __init__(self) -> None:
            self.browser_artifacts = BrowserArtifacts()

        async def get_working_page(self) -> object:
            return _CheckoutPage()

    class FakeWorkflowRunContext:
        values: dict[str, object] = {}
        secrets: dict[str, object] = {}
        include_secrets_in_templates = False
        organization_id = None
        workflow_title = "Checkout"
        workflow_id = "w_checkout"
        workflow_permanent_id = "wpid_checkout"
        workflow_run_id = "wr_executor_witness"
        browser_session_id = None
        workflow_run_outputs: list[object] = []
        workflow = None

        def get_block_metadata(self, label: str | None) -> dict[str, object]:
            return {}

        def build_workflow_run_summary(self) -> str:
            return ""

        def mask_secrets_in_data(self, data: object, mask: str = "*****") -> object:
            return data

    async def noop(*args: object, **kwargs: object) -> None:
        return None

    async def browser_state(*args: object, **kwargs: object) -> FakeBrowserState:
        return FakeBrowserState()

    monkeypatch.setattr("skyvern.forge.sdk.workflow.models.block.app.AGENT_FUNCTION.validate_code_block", noop)
    monkeypatch.setattr(CodeBlock, "get_or_create_browser_state", browser_state)
    monkeypatch.setattr(CodeBlock, "get_workflow_run_context", lambda *args: FakeWorkflowRunContext())
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", noop)

    now = datetime.now(UTC)
    block = CodeBlock(
        label="collect_payment_options",
        code=code,
        output_parameter=OutputParameter(
            parameter_type=ParameterType.OUTPUT,
            key="collect_payment_options_output",
            description="checkout facts",
            output_parameter_id="op_checkout",
            workflow_id="w_checkout",
            created_at=now,
            modified_at=now,
        ),
    )
    result = await block.execute(workflow_run_id="wr_executor_witness", workflow_run_block_id="")
    assert result.success is True, "the fixture block must execute"
    assert isinstance(result.output_parameter_value, dict)
    return result.output_parameter_value


def _executor_witness_ctx() -> CopilotContext:
    ctx = _locator_packet_ctx()
    ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
    ctx.code_artifact_metadata = {
        "collect_payment_options": {
            "claimed_outcomes": [
                {
                    "id": "checkout",
                    "goal_value_paths": ["$.cart_line_item", "$.payment_options", "$.selection_output"],
                }
            ]
        }
    }
    return ctx


@pytest.mark.asyncio
async def test_the_repaired_block_executes_and_the_run_owns_the_outputs_it_was_asked_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole chain on executed code: a completed run that owns nothing names the paths it owes,
    and the repaired block's own execution output clears them."""
    before_output = await _execute_code_block(monkeypatch, _UNREPAIRED_CODE)
    after_output = await _execute_code_block(monkeypatch, _REPAIRED_CODE)

    # The outputs under test came out of the executor, not out of a fixture file.
    assert set(before_output) == {"url", "actions"}
    assert after_output["payment_options"] == ["Card", "Wallet", "Pay in 4"]
    assert after_output["cart_line_item"] == {
        "name": "Running Jacket",
        "size": "Size M",
        "quantity": "Qty 1",
        "price": "88.00",
    }

    def recorded(output: dict[str, object]) -> tuple[CopilotContext, object]:
        ctx = _executor_witness_ctx()
        result = {
            "ok": True,
            "data": {
                "workflow_run_id": "wr_executor_witness",
                "overall_status": "completed",
                "requested_block_labels": ["collect_payment_options"],
                "executed_block_labels": ["collect_payment_options"],
                "blocks": [
                    {
                        "label": "collect_payment_options",
                        "block_type": "code",
                        "status": "completed",
                        "output": output,
                        "extracted_data": output,
                    }
                ],
                # The value the executor produced, on the surface the runtime registers it on.
                "registered_output_parameter_values": [
                    {
                        "workflow_run_id": "wr_executor_witness",
                        "output_parameter_id": "op_checkout",
                        "output_parameter_key": "collect_payment_options_output",
                        "block_label": "collect_payment_options",
                        "block_type": "code",
                        "value": output,
                    }
                ],
            },
        }
        run_execution_module._record_run_blocks_result(ctx, result, completion_verification=None)
        return ctx, result

    before_ctx, _before_result = recorded(before_output)
    after_ctx, after_result = recorded(after_output)

    before_outcome = before_ctx.latest_recorded_build_test_outcome
    assert before_outcome is not None
    assert before_outcome.verdict == "repairable_failure"
    assert sorted(fact["output_path"] for fact in before_outcome.missing_requested_output_facts) == [
        "cart_line_item",
        "payment_options",
        "selection_output",
    ]
    rendered = _recorded_build_test_outcome_prompt(before_ctx)
    for path in ("cart_line_item", "payment_options", "selection_output"):
        assert f"output_path={path}" in rendered

    after_outcome = after_ctx.latest_recorded_build_test_outcome
    assert after_outcome is not None
    assert after_outcome.missing_requested_output_facts == []
    assert after_outcome.verdict == "not_authoritative"

    # The corrected run hands back the values its own code produced.
    packet = project_build_test_packet_for_llm(
        build_test_evidence_packet(after_ctx, after_result, recorded_outcome=after_outcome)
    ).model_dump(mode="json", exclude_none=True)
    assert packet["unfinished_items"] == []
    run_owned = [
        output["output"] for output in packet["registered_outputs"] if output["label"] == "collect_payment_options"
    ]
    assert run_owned == [after_output], "the corrected run does not hand back the output its code produced"


@pytest.mark.asyncio
async def test_completed_run_is_recorded_before_the_browser_enrichment_await(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completed_block = terminal_extraction_block("completed").model_copy(update={"output": {"heading": "Done"}})
    ctx = await handback_ctx(
        monkeypatch,
        polled_status="completed",
        block_status="completed",
        terminal_blocks=[completed_block],
    )
    order: list[str] = []
    projected_rows_at_commit: list[list[dict[str, object]]] = []
    real_record = run_execution_module.record_build_test_outcome
    real_commit = run_execution_module._commit_run_blocks_record

    def _record(record_ctx: object, outcome: object) -> None:
        order.append("record")
        real_record(record_ctx, outcome)

    def _commit(commit_ctx: CopilotContext, result: dict[str, object]) -> RecordedRunOutcome | None:
        packet = build_test_evidence_packet(commit_ctx, result)
        projected_rows_at_commit.append(
            [output.model_dump(mode="json", exclude_none=True) for output in packet.registered_outputs]
        )
        return real_commit(commit_ctx, result)

    async def _enrichment(*_args: object, **_kwargs: object) -> tuple[str, dict[str, object] | None]:
        order.append("enrichment")
        return "", None

    monkeypatch.setattr(run_execution_module, "record_build_test_outcome", _record)
    monkeypatch.setattr(run_execution_module, "_commit_run_blocks_record", _commit)
    monkeypatch.setattr(run_execution_module, "_attach_post_run_browser_enrichment", _enrichment)

    result = await _run_blocks_and_collect_debug({"block_labels": ["extract_heading"], "parameters": {}}, ctx)

    assert result["ok"] is True, result
    assert order == ["record", "enrichment"]
    assert projected_rows_at_commit == [
        [
            {
                "label": "extract_heading",
                "status": "completed",
                "output": {"heading": "Done"},
                "value_complete": True,
            }
        ]
    ]
    assert ctx.latest_recorded_build_test_outcome is not None
    assert ctx.latest_recorded_build_test_outcome.workflow_run_id == "wr_paused"


@pytest.mark.asyncio
async def test_failed_run_still_records_the_failure_and_marks_the_post_run_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = await handback_ctx(monkeypatch, polled_status="failed", block_status="failed")
    marks: list[str] = []

    async def _enrichment(*_args: object, **_kwargs: object) -> tuple[str, dict[str, object] | None]:
        return "", None

    monkeypatch.setattr(run_execution_module, "_attach_post_run_browser_enrichment", _enrichment)
    monkeypatch.setattr(
        run_execution_module,
        "_mark_stored_post_run_failure_page",
        lambda _ctx: marks.append("marked"),
    )
    counts = count_record_and_send(monkeypatch)

    result = await _run_blocks_and_collect_debug({"block_labels": ["extract_heading"], "parameters": {}}, ctx)
    await _verify_and_record_run_blocks_result(ctx, result, 0.0)

    assert result["ok"] is False, result
    outcome = ctx.latest_recorded_build_test_outcome
    assert outcome is not None
    assert outcome.workflow_run_id == "wr_paused"
    assert outcome.verdict == "repairable_failure"
    assert outcome.reason_code != ""
    assert marks == ["marked"]
    assert counts == {"record": 1, "send": 1}
    assert len(ctx.recorded_build_test_outcome_history) == 1


@pytest.mark.asyncio
async def test_enrichment_facts_still_land_on_the_recorded_outcome_and_agree_with_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = await handback_ctx(monkeypatch, polled_status="completed", block_status="completed")
    evidence = same_run_page_evidence()

    async def _enrichment(
        enrich_ctx: CopilotContext, result_data: dict[str, object], **_kwargs: object
    ) -> tuple[str, dict[str, object]]:
        enrich_ctx.composition_page_evidence = evidence
        result_data["current_url"] = "https://example.com/done"
        result_data["post_run_page_evidence"] = evidence
        result_data["post_run_page_capture"] = {"status": "captured"}
        return "https://example.com/done", evidence

    monkeypatch.setattr(run_execution_module, "_attach_post_run_browser_enrichment", _enrichment)
    monkeypatch.setattr(
        run_execution_module.app.AGENT_FUNCTION,
        "captcha_solving_available",
        AsyncMock(return_value=True),
    )
    counts = count_record_and_send(monkeypatch)

    result = await _run_blocks_and_collect_debug({"block_labels": ["extract_heading"], "parameters": {}}, ctx)
    await _verify_and_record_run_blocks_result(ctx, result, 0.0)

    outcome = ctx.latest_recorded_build_test_outcome
    assert outcome is not None
    assert ctx.captcha_solver_available is True
    assert ctx.captcha_solver_available_for_url == "https://example.com/done"
    assert outcome.page_evidence_refs, outcome
    assert outcome.page_capture is not None and outcome.page_capture.status == "captured"
    assert counts == {"record": 1, "send": 1}
    assert len(ctx.recorded_build_test_outcome_history) == 1
    entry = ctx.recorded_build_test_outcome_history[-1]
    assert entry["workflow_run_id"] == outcome.workflow_run_id
    assert entry["verdict"] == outcome.verdict
    assert entry["reason_code"] == outcome.reason_code
    assert entry["structural_key"] == outcome.structural_key
    assert outcome.is_authoritative is True
    assert entry["is_authoritative"] is True
    assert ctx.recorded_persisted_block_run_workflow_run_id == outcome.workflow_run_id


@pytest.mark.asyncio
async def test_failed_run_known_only_by_its_page_is_graded_after_enrichment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed block carrying no failure_reason has no structural identity until the post-run page
    arrives, so grading it before enrichment records nothing at all."""
    ctx = await handback_ctx(
        monkeypatch,
        polled_status="failed",
        block_status="failed",
        terminal_blocks=[page_only_failed_block()],
    )
    evidence = same_run_page_evidence()

    async def _enrichment(
        enrich_ctx: CopilotContext, result_data: dict[str, object], **_kwargs: object
    ) -> tuple[str, dict[str, object]]:
        enrich_ctx.composition_page_evidence = evidence
        result_data["post_run_page_evidence"] = evidence
        return "https://example.com/done", evidence

    monkeypatch.setattr(run_execution_module, "_attach_post_run_browser_enrichment", _enrichment)
    counts = count_record_and_send(monkeypatch)

    result = await _run_blocks_and_collect_debug({"block_labels": ["extract_heading"], "parameters": {}}, ctx)
    await _verify_and_record_run_blocks_result(ctx, result, 0.0)

    outcome = ctx.latest_recorded_build_test_outcome
    assert outcome is not None, "the run's failure was lost because the page was unknown at record time"
    assert outcome.verdict == "repairable_failure", outcome
    assert outcome.page_evidence_refs, outcome
    assert outcome.key_provenance.get("structural_failure_identity") != (
        "no typed verification/page/output identity available"
    )
    assert counts["record"] == 1
    assert len(ctx.recorded_build_test_outcome_history) == 1
    entry = ctx.recorded_build_test_outcome_history[-1]
    assert entry["verdict"] == outcome.verdict
    assert entry["reason_code"] == outcome.reason_code
    assert entry["structural_key"] == outcome.structural_key
    assert entry["is_authoritative"] == outcome.is_authoritative


@pytest.mark.asyncio
async def test_watchdog_paused_result_is_recorded_before_the_captcha_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = await handback_ctx(monkeypatch, polled_status="paused", block_status="completed")
    record_build_test_outcome(ctx, failed_second_factor_run("wr_prior_failure"))
    counts = count_record_and_send(monkeypatch)
    observed_at_probe: list[RecordedBuildTestOutcome | None] = []

    async def _cancelled_probe(*_args: object, **_kwargs: object) -> bool:
        observed_at_probe.append(ctx.latest_recorded_build_test_outcome)
        raise asyncio.CancelledError

    monkeypatch.setattr(
        run_execution_module.app.AGENT_FUNCTION,
        "captcha_solving_available",
        _cancelled_probe,
    )

    result = await _run_blocks_and_collect_debug({"block_labels": ["extract_heading"], "parameters": {}}, ctx)

    assert result["ok"] is False, result
    assert result["data"]["control_signal"]["kind"] == "watchdog_paused"
    assert _INTERNAL_RUN_OUTCOME_RECORDED_KEY not in result

    with pytest.raises(asyncio.CancelledError):
        await _verify_and_record_run_blocks_result(ctx, result, 0.0)

    assert len(observed_at_probe) == 1
    at_probe = observed_at_probe[0]
    assert at_probe is not None and at_probe.workflow_run_id == "wr_paused"
    assert at_probe.verdict == "not_authoritative"
    outcome = ctx.latest_recorded_build_test_outcome
    assert outcome is not None and outcome.workflow_run_id == "wr_paused"
    assert counts == {"record": 1, "send": 1}
    assert [entry["workflow_run_id"] for entry in ctx.recorded_build_test_outcome_history] == [
        "wr_prior_failure",
        "wr_paused",
    ]


def _signature_only_failure(run_id: str) -> RecordedBuildTestOutcome:
    return RecordedBuildTestOutcome(
        phase="persisted_block_run",
        attempted_tool="update_and_run_blocks",
        attempted_block_label="sign_in_and_read",
        verdict="repairable_failure",
        reason_code="runtime_block_failure",
        workflow_run_id=run_id,
        block_labels=["sign_in_and_read"],
        structural_failure_identity="value-error-identity",
    )


def _executing_run(
    run_id: str,
    executed_block_labels: list[str],
    *,
    verdict: str = "progress_observed",
    reason_code: str = "",
    failed_operation: BuildTestFailedOperation | None = None,
    failed_block_labels: list[str] | None = None,
) -> RecordedBuildTestOutcome:
    default_reason_code = "verified_success" if verdict == "progress_observed" else "run_completed_unevaluated"
    return RecordedBuildTestOutcome(
        phase="persisted_block_run",
        attempted_tool="update_and_run_blocks",
        verdict=verdict,  # type: ignore[arg-type]
        reason_code=reason_code or default_reason_code,  # type: ignore[arg-type]
        workflow_run_id=run_id,
        block_labels=["sign_in_and_read"],
        executed_block_labels=executed_block_labels,
        failed_block_labels=failed_block_labels or [],
        evidence_refs=["rows:1"],
        failed_operation=failed_operation,
    )


def _executed_snapshot_history(
    failure: RecordedBuildTestOutcome,
    later_run: RecordedBuildTestOutcome,
    *,
    executed_yaml: str,
    failing_yaml: str = "",
) -> SimpleNamespace:
    ctx = _run_history_ctx(failing_yaml or two_page_login_yaml())
    ctx.persisted_workflow_yaml = None
    record_build_test_outcome(ctx, failure)
    ctx.workflow_yaml = executed_yaml
    record_build_test_outcome(ctx, later_run)
    return ctx


def test_a_run_over_a_changed_snapshot_that_drops_the_failing_call_clears_the_failure() -> None:
    """The snapshot a run executed is evidence the delivered workflow cannot supply when nothing is
    persisted yet, and it is bound when that run is recorded rather than read back off the draft."""
    ctx = _executed_snapshot_history(
        failed_second_factor_run("wr_1"),
        _executing_run("wr_2", ["sign_in_and_read"]),
        executed_yaml=two_page_login_yaml(submit_selector="Continue"),
    )

    unresolved, disposition = unresolved_runtime_block_failure_with_disposition(
        ctx, reported_workflow_yaml=None, reported_workflow_is_persisted=True
    )

    assert unresolved is None
    assert disposition == "executed_snapshot_call_removed:wr_2:sign_in_and_read"
    assert [entry["workflow_run_id"] for entry in ctx.recorded_build_test_outcome_history] == ["wr_1", "wr_2"]
    assert ctx.recorded_build_test_outcome_history[0]["reason_code"] == "runtime_block_failure"


def test_a_run_over_a_changed_snapshot_clears_a_failure_that_named_no_call() -> None:
    ctx = _executed_snapshot_history(
        _signature_only_failure("wr_1"),
        _executing_run("wr_2", ["sign_in_and_read"]),
        executed_yaml=straight_line_login_yaml(),
    )

    unresolved, disposition = unresolved_runtime_block_failure_with_disposition(
        ctx, reported_workflow_yaml=None, reported_workflow_is_persisted=True
    )

    assert unresolved is None
    assert disposition == "executed_snapshot_signature_changed:wr_2:sign_in_and_read"


def test_a_later_run_that_executed_no_block_still_carries_the_failure() -> None:
    ctx = _executed_snapshot_history(
        failed_second_factor_run("wr_1"),
        _executing_run("wr_2", []),
        executed_yaml=two_page_login_yaml(submit_selector="Continue"),
    )

    unresolved, disposition = unresolved_runtime_block_failure_with_disposition(
        ctx, reported_workflow_yaml=None, reported_workflow_is_persisted=True
    )

    assert unresolved == UnresolvedRuntimeFailure(workflow_run_id="wr_1", block_label="sign_in_and_read")
    assert disposition == "no_reported_candidate"


def test_a_later_run_covering_only_a_sibling_block_still_carries_the_failure() -> None:
    ctx = _executed_snapshot_history(
        failed_second_factor_run("wr_1"),
        _executing_run("wr_2", ["read_metric"]),
        executed_yaml=two_page_login_yaml(submit_selector="Continue"),
    )

    unresolved, disposition = unresolved_runtime_block_failure_with_disposition(
        ctx, reported_workflow_yaml=None, reported_workflow_is_persisted=True
    )

    assert unresolved == UnresolvedRuntimeFailure(workflow_run_id="wr_1", block_label="sign_in_and_read")
    assert disposition == "no_reported_candidate"


def test_a_later_run_over_the_same_source_still_carries_the_failure() -> None:
    ctx = _executed_snapshot_history(
        failed_second_factor_run("wr_1"),
        _executing_run("wr_2", ["sign_in_and_read"]),
        executed_yaml=two_page_login_yaml(),
    )

    unresolved, disposition = unresolved_runtime_block_failure_with_disposition(
        ctx, reported_workflow_yaml=None, reported_workflow_is_persisted=True
    )

    assert unresolved == UnresolvedRuntimeFailure(workflow_run_id="wr_1", block_label="sign_in_and_read")
    assert disposition == "no_reported_candidate"


def test_a_later_run_over_a_snapshot_of_runtime_built_selectors_still_carries_the_failure() -> None:
    ctx = _executed_snapshot_history(
        failed_second_factor_run("wr_1").model_copy(update={"attempted_call_ref": "locator:#submit-btn"}),
        _executing_run("wr_2", ["sign_in_and_read"]),
        executed_yaml=_templated_selector_yaml(),
    )

    unresolved, disposition = unresolved_runtime_block_failure_with_disposition(
        ctx, reported_workflow_yaml=None, reported_workflow_is_persisted=True
    )

    assert unresolved == UnresolvedRuntimeFailure(workflow_run_id="wr_1", block_label="sign_in_and_read")
    assert disposition == "no_reported_candidate"


def test_a_later_run_over_the_same_source_still_carries_a_failure_that_named_no_call() -> None:
    ctx = _executed_snapshot_history(
        _signature_only_failure("wr_1"),
        _executing_run("wr_2", ["sign_in_and_read"]),
        executed_yaml=two_page_login_yaml(),
    )

    unresolved, disposition = unresolved_runtime_block_failure_with_disposition(
        ctx, reported_workflow_yaml=None, reported_workflow_is_persisted=True
    )

    assert unresolved == UnresolvedRuntimeFailure(workflow_run_id="wr_1", block_label="sign_in_and_read")
    assert disposition == "no_reported_candidate"


def test_a_later_run_whose_snapshot_carries_no_such_block_still_carries_the_failure() -> None:
    ctx = _executed_snapshot_history(
        _signature_only_failure("wr_1"),
        _executing_run("wr_2", ["sign_in_and_read"]),
        executed_yaml="title: Sign in and read the metric\nworkflow_definition:\n  blocks: []\n",
    )

    unresolved, disposition = unresolved_runtime_block_failure_with_disposition(
        ctx, reported_workflow_yaml=None, reported_workflow_is_persisted=True
    )

    assert unresolved == UnresolvedRuntimeFailure(workflow_run_id="wr_1", block_label="sign_in_and_read")
    assert disposition == "no_reported_candidate"


@pytest.mark.parametrize(
    "reason_code",
    [
        "unrecoverable_tool_error",
        "terminal_challenge_blocker",
        "no_meaningful_output",
        "fallback_floor_turn_unsatisfiable",
    ],
)
def test_a_later_run_that_reached_no_evaluated_completion_still_carries_the_failure(reason_code: str) -> None:
    ctx = _executed_snapshot_history(
        failed_second_factor_run("wr_1"),
        _executing_run("wr_2", ["sign_in_and_read"], verdict="not_authoritative", reason_code=reason_code),
        executed_yaml=two_page_login_yaml(submit_selector="Continue"),
    )

    unresolved, disposition = unresolved_runtime_block_failure_with_disposition(
        ctx, reported_workflow_yaml=None, reported_workflow_is_persisted=True
    )

    assert unresolved == UnresolvedRuntimeFailure(workflow_run_id="wr_1", block_label="sign_in_and_read")
    assert disposition == "no_reported_candidate"


def test_a_later_completed_run_whose_own_block_failed_still_carries_the_failure() -> None:
    ctx = _executed_snapshot_history(
        failed_second_factor_run("wr_1"),
        _executing_run(
            "wr_2",
            ["sign_in_and_read"],
            verdict="not_authoritative",
            failed_operation=BuildTestFailedOperation(
                kind="browser_operation_failed",
                workflow_run_id="wr_2",
                block_label="sign_in_and_read",
            ),
        ),
        executed_yaml=two_page_login_yaml(submit_selector="Continue"),
    )

    unresolved, disposition = unresolved_runtime_block_failure_with_disposition(
        ctx, reported_workflow_yaml=None, reported_workflow_is_persisted=True
    )

    assert unresolved == UnresolvedRuntimeFailure(workflow_run_id="wr_1", block_label="sign_in_and_read")
    assert disposition == "no_reported_candidate"


def test_a_completed_run_that_evaluated_no_output_clears_over_a_changed_snapshot() -> None:
    ctx = _executed_snapshot_history(
        failed_second_factor_run("wr_1"),
        _executing_run("wr_2", ["sign_in_and_read"], verdict="not_authoritative"),
        executed_yaml=two_page_login_yaml(submit_selector="Continue"),
    )

    unresolved, disposition = unresolved_runtime_block_failure_with_disposition(
        ctx, reported_workflow_yaml=None, reported_workflow_is_persisted=True
    )

    assert unresolved is None
    assert disposition == "executed_snapshot_call_removed:wr_2:sign_in_and_read"


def test_the_tool_result_and_the_terminal_agree_once_the_executed_snapshot_cleared_the_failure() -> None:
    ctx = make_copilot_ctx(workflow_yaml=two_page_login_yaml())
    ctx.persisted_workflow_yaml = None
    record_build_test_outcome(ctx, failed_second_factor_run("wr_1"))
    ctx.workflow_yaml = two_page_login_yaml(submit_selector="Continue")
    record_build_test_outcome(ctx, _executing_run("wr_2", ["sign_in_and_read"]))
    ctx.last_run_blocks_workflow_run_id = "wr_2"
    result = _run_result_ok()

    run_execution_module._carry_unresolved_failure_into_result(ctx, result, "update_and_run_blocks")
    terminal = _make_agent_result(
        ctx,
        user_response="Built it and tested it.",
        updated_workflow=object(),
        global_llm_context=None,
        turn_outcome=TurnOutcome(response_kind=ResponseKind.BUILD),
        narrative_payload={"terminalMessage": "Built it and tested it.", "narrativeSummary": "Built it and tested it."},
    )

    assert "unresolved_earlier_failure" not in result["data"]
    assert terminal.turn_outcome is not None
    assert terminal.turn_outcome.unresolved_runtime_failure is None
    assert terminal.narrative_payload is not None
    assert terminal.narrative_payload["terminalMessage"] == "Built it and tested it."


def _many_literal_selectors_yaml(selector_count: int) -> str:
    calls = "".join(f'          await page.locator("#field-{index}").click()\n' for index in range(selector_count))
    return (
        "title: Sign in and read the metric\n"
        "workflow_definition:\n"
        "  blocks:\n"
        "  - block_type: code\n"
        "    label: sign_in_and_read\n"
        "    code: |\n" + calls
    )


def test_reverting_the_draft_to_the_code_that_failed_carries_the_failure_again() -> None:
    """A clean run proves nothing about source it no longer describes, so a revert re-carries."""
    ctx = _executed_snapshot_history(
        failed_second_factor_run("wr_1"),
        _executing_run("wr_2", ["sign_in_and_read"]),
        executed_yaml=two_page_login_yaml(submit_selector="Continue"),
    )
    ctx.workflow_yaml = two_page_login_yaml()

    unresolved, disposition = unresolved_runtime_block_failure_with_disposition(
        ctx, reported_workflow_yaml=None, reported_workflow_is_persisted=True
    )

    assert unresolved == UnresolvedRuntimeFailure(workflow_run_id="wr_1", block_label="sign_in_and_read")
    assert disposition == "no_reported_candidate"


def test_a_signature_change_on_a_snapshot_the_draft_no_longer_holds_carries_the_failure() -> None:
    """The signature arm is bound to the source being judged the same way the call arm is."""
    ctx = _executed_snapshot_history(
        _signature_only_failure("wr_1"),
        _executing_run("wr_2", ["sign_in_and_read"]),
        executed_yaml=straight_line_login_yaml(),
    )
    ctx.workflow_yaml = two_page_login_yaml()

    unresolved, disposition = unresolved_runtime_block_failure_with_disposition(
        ctx, reported_workflow_yaml=None, reported_workflow_is_persisted=True
    )

    assert unresolved == UnresolvedRuntimeFailure(workflow_run_id="wr_1", block_label="sign_in_and_read")
    assert disposition == "no_reported_candidate"


def test_a_snapshot_with_more_calls_than_the_evidence_holds_cannot_prove_removal() -> None:
    """Past the cap the ref list is incomplete, and an incomplete list cannot show a call is gone."""
    ctx = _executed_snapshot_history(
        failed_second_factor_run("wr_1"),
        _executing_run("wr_2", ["sign_in_and_read"]),
        executed_yaml=_many_literal_selectors_yaml(_EXECUTED_CALL_REF_LIMIT + 6),
    )
    evidence = ctx.recorded_build_test_outcome_history[-1]["executed_block_evidence"]["sign_in_and_read"]

    unresolved, disposition = unresolved_runtime_block_failure_with_disposition(
        ctx, reported_workflow_yaml=None, reported_workflow_is_persisted=True
    )

    assert len(evidence["call_refs"]) == _EXECUTED_CALL_REF_LIMIT
    assert evidence["call_refs_truncated"] is True
    assert evidence["removal_provable"] is False
    assert unresolved == UnresolvedRuntimeFailure(workflow_run_id="wr_1", block_label="sign_in_and_read")
    assert disposition == "no_reported_candidate"


def test_a_staged_repair_does_not_clear_a_failure_the_saved_workflow_still_carries() -> None:
    """A repair the user has not saved leaves the workflow they can run carrying the failure."""
    saved = two_page_login_yaml()
    ctx = _run_history_ctx(saved)
    record_build_test_outcome(ctx, failed_second_factor_run("wr_1"))
    ctx.staged_workflow_yaml = two_page_login_yaml(submit_selector="Continue")
    record_build_test_outcome(ctx, _executing_run("wr_2", ["sign_in_and_read"]))
    ctx.staged_workflow_yaml = None

    unresolved, disposition = unresolved_runtime_block_failure_with_disposition(ctx, reported_workflow_yaml=saved)

    assert unresolved == UnresolvedRuntimeFailure(workflow_run_id="wr_1", block_label="sign_in_and_read")
    assert disposition == "unresolved"


def test_a_run_over_a_changed_staged_proposal_clears_when_nothing_is_persisted() -> None:
    """The ticket's shape: the proposal that ran was never saved, so only its own receipts prove the repair."""
    ctx = _run_history_ctx(two_page_login_yaml())
    ctx.persisted_workflow_yaml = None
    record_build_test_outcome(ctx, failed_second_factor_run("wr_1"))
    # Stays staged: the terminal is assembled before the route commits the proposal.
    ctx.staged_workflow_yaml = two_page_login_yaml(submit_selector="Continue")
    record_build_test_outcome(ctx, _executing_run("wr_2", ["sign_in_and_read"]))

    unresolved, disposition = unresolved_runtime_block_failure_with_disposition(
        ctx, reported_workflow_yaml=None, reported_workflow_is_persisted=True
    )

    assert unresolved is None
    assert disposition == "executed_snapshot_call_removed:wr_2:sign_in_and_read"


def test_a_caller_judging_a_proposal_does_not_read_the_draft_when_it_has_none() -> None:
    """An absent proposal means the caller has nothing to show, not that nothing is saved."""
    ctx = _executed_snapshot_history(
        failed_second_factor_run("wr_1"),
        _executing_run("wr_2", ["sign_in_and_read"]),
        executed_yaml=two_page_login_yaml(submit_selector="Continue"),
    )

    unresolved, disposition = unresolved_runtime_block_failure_with_disposition(ctx, reported_workflow_yaml=None)

    assert unresolved == UnresolvedRuntimeFailure(workflow_run_id="wr_1", block_label="sign_in_and_read")
    assert disposition == "no_reported_candidate"


def test_a_completed_run_whose_block_failed_without_a_browser_error_still_carries_the_failure() -> None:
    """``failed_operation`` is only set for browser failures, so it cannot mean no block failed."""
    ctx = _executed_snapshot_history(
        failed_second_factor_run("wr_1"),
        _executing_run(
            "wr_2",
            ["sign_in_and_read"],
            verdict="not_authoritative",
            failed_block_labels=["sign_in_and_read"],
        ),
        executed_yaml=two_page_login_yaml(submit_selector="Continue"),
    )

    unresolved, disposition = unresolved_runtime_block_failure_with_disposition(
        ctx, reported_workflow_yaml=None, reported_workflow_is_persisted=True
    )

    assert unresolved == UnresolvedRuntimeFailure(workflow_run_id="wr_1", block_label="sign_in_and_read")
    assert disposition == "no_reported_candidate"


def _adjacent_literal_selector_yaml() -> str:
    """Adjacent string literals parse to one constant, which the text scan reads as a shorter selector."""
    return """
    title: Sign in and read the metric
    workflow_definition:
      blocks:
      - block_type: code
        label: sign_in_and_read
        code: |
          await page.locator("#sub" "mit").click()
          return {"visitors": "9.42K"}
    """


def test_a_selector_the_text_scan_cannot_read_whole_cannot_prove_removal() -> None:
    """The scan records ``#sub`` while the call runs ``#submit``; absence from the scan is not removal."""
    ctx = _executed_snapshot_history(
        failed_second_factor_run("wr_1"),
        _executing_run("wr_2", ["sign_in_and_read"]),
        executed_yaml=_adjacent_literal_selector_yaml(),
    )

    evidence = ctx.recorded_build_test_outcome_history[-1]["executed_block_evidence"]["sign_in_and_read"]

    assert evidence["call_refs"] == ["locator:#sub"]
    assert evidence["removal_provable"] is False


def test_a_later_failed_block_is_captured_so_it_cannot_witness_its_own_repair() -> None:
    """Capturing only the first failed block let a second one mint evidence and clear its own failure."""
    outcome = recorded_outcome_from_run_blocks_result(
        {
            "ok": True,
            "data": {
                "workflow_run_id": "wr_2",
                "blocks": [
                    {"label": "open_page", "status": "failed", "failure_type": "runtime_error"},
                    {"label": "sign_in_and_read", "status": "failed", "failure_type": "runtime_error"},
                ],
            },
        },
        recorded_run_outcome=RecordedRunOutcome(verdict="not_evaluated", workflow_run_id="wr_2"),
    )

    assert outcome is not None
    assert outcome.failed_block_labels == ["open_page", "sign_in_and_read"]

    ctx = _run_history_ctx(two_page_login_yaml())
    ctx.persisted_workflow_yaml = None
    record_build_test_outcome(ctx, failed_second_factor_run("wr_1"))
    ctx.workflow_yaml = two_page_login_yaml(submit_selector="Continue")
    record_build_test_outcome(ctx, outcome)

    unresolved, disposition = unresolved_runtime_block_failure_with_disposition(
        ctx, reported_workflow_yaml=None, reported_workflow_is_persisted=True
    )

    assert unresolved == UnresolvedRuntimeFailure(workflow_run_id="wr_1", block_label="sign_in_and_read")
    assert disposition == "no_reported_candidate"


def _runtime_built_role_name_yaml() -> str:
    """A per-account accessible name built at runtime, which the literal scan cannot see."""
    return """
    title: Sign in and read the metric
    workflow_definition:
      blocks:
      - block_type: code
        label: sign_in_and_read
        code: |
          account = "acct-42"
          await page.get_by_role("button", name=account).click()
          return {"visitors": "9.42K"}
    """


def test_a_selector_argument_built_at_runtime_cannot_prove_removal() -> None:
    """The role alone resolves to an identity the scan did record, so only the literal count is left
    to separate a dynamic argument from a call the scan read whole."""
    ctx = _executed_snapshot_history(
        failed_second_factor_run("wr_1"),
        _executing_run("wr_2", ["sign_in_and_read"]),
        executed_yaml=_runtime_built_role_name_yaml(),
    )

    evidence = ctx.recorded_build_test_outcome_history[-1]["executed_block_evidence"]["sign_in_and_read"]

    assert evidence["removal_provable"] is False

    unresolved, disposition = unresolved_runtime_block_failure_with_disposition(
        ctx, reported_workflow_yaml=None, reported_workflow_is_persisted=True
    )

    assert unresolved == UnresolvedRuntimeFailure(workflow_run_id="wr_1", block_label="sign_in_and_read")
    assert disposition == "no_reported_candidate"


@pytest.mark.asyncio
async def test_a_foreign_occupier_stops_the_dispatch_before_any_workflow_run_is_created(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = await install_run_blocks_harness(
        monkeypatch,
        workflow_yaml=HANDBACK_WORKFLOW_YAML,
        polled_status="running",
        dispatch_to_worker=True,
    )
    now = datetime.now(UTC)
    sessions_manager = MagicMock()
    sessions_manager.get_session = AsyncMock(
        return_value=PersistentBrowserSession(
            persistent_browser_session_id="pbs_chat",
            organization_id="org-1",
            runnable_id="wr_foreign",
            runnable_type="workflow_run",
            status="running",
            created_at=now,
            modified_at=now,
        )
    )
    monkeypatch.setattr(forge_app, "PERSISTENT_SESSIONS_MANAGER", sessions_manager)
    forge_app.WORKFLOW_SERVICE.mark_workflow_run_as_canceled_if_not_final = AsyncMock()
    forge_app.DATABASE.workflow_runs.get_workflow_run = AsyncMock(
        return_value=SimpleNamespace(
            workflow_run_id="wr_foreign",
            copilot_session_id="wcc_someone_else",
            status=WorkflowRunStatus.running,
            parent_workflow_run_id=None,
        )
    )

    @asynccontextmanager
    async def _attach(_ctx: CopilotContext) -> AsyncIterator[None]:
        yield

    monkeypatch.setattr(runtime, "mcp_browser_context", _attach)
    monkeypatch.setattr(runtime, "_drop_browser_session_id_at_its_fixed_deadline", AsyncMock())

    ctx = make_copilot_ctx(browser_session_id="pbs_chat")
    ctx.staged_workflow = harness["workflow"]
    ctx.workflow_copilot_chat_id = "wcc_mine"

    result = await _run_blocks_and_collect_debug({"block_labels": ["extract_heading"], "parameters": {}}, ctx)

    workflow_service_module.prepare_workflow.assert_not_awaited()
    harness["worker_execute"].assert_not_awaited()
    forge_app.WORKFLOW_SERVICE.execute_workflow.assert_not_awaited()
    forge_app.WORKFLOW_SERVICE.mark_workflow_run_as_canceled_if_not_final.assert_not_awaited()
    assert result["ok"] is False, result
    assert result["data"]["build_test_connect_failure"]["state"] == "occupied"
    assert result["data"]["build_test_connect_failure"]["occupier_run_id"] == "wr_foreign"
    assert result["data"]["executed_block_labels"] == []


@pytest.mark.asyncio
async def test_budget_denied_dispatch_preserves_prior_run_through_public_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_copilot_ctx()
    ctx.budget_expiry_state.source = "deadline"
    ctx.budget_expiry_state.drain_active = True
    ctx.last_run_blocks_workflow_run_id = "wr_prior"
    ctx.last_successful_run_blocks_workflow_run_id = "wr_prior"
    ctx.last_run_blocks_browser_session_id = "pbs_prior"
    ctx.last_test_ok = True
    ctx.last_full_workflow_test_ok = True
    ctx.last_run_outcome = RecordedRunOutcome(
        verdict="verified", reason_code="execution_completed", workflow_run_id="wr_prior", run_completed=True
    )
    previous_outcome = ctx.last_run_outcome
    previous_history = list(ctx.terminal_envelope_run_outcomes)
    previous_diagnosis = ctx.latest_diagnosis_repair_contract
    monkeypatch.setattr(tools_module, "_authority_tool_error", lambda *_args: None)
    monkeypatch.setattr(tools_module, "_get_prior_workflow_definition", AsyncMock(return_value=None))
    monkeypatch.setattr(tools_module, "_plan_frontier", lambda *_args: (["step"], {}, "step", "initial"))
    dispatch = AsyncMock(side_effect=AssertionError("denied dispatch must not reach the database"))
    monkeypatch.setattr(run_execution_module.app.DATABASE.workflows, "get_workflow_by_permanent_id", dispatch)

    result = json.loads(
        await tools_module.run_blocks_tool.on_invoke_tool(
            SimpleNamespace(context=ctx, tool_name="run_blocks_and_collect_debug"),
            json.dumps({"block_labels": ["step"], "parameters": {}}),
        )
    )

    assert ctx.last_run_blocks_workflow_run_id == "wr_prior"
    assert ctx.last_successful_run_blocks_workflow_run_id == "wr_prior"
    assert ctx.last_run_blocks_browser_session_id == "pbs_prior"
    assert ctx.last_test_ok is True
    assert ctx.last_full_workflow_test_ok is True
    assert ctx.last_run_outcome is previous_outcome
    assert ctx.terminal_envelope_run_outcomes == previous_history
    assert ctx.latest_diagnosis_repair_contract is previous_diagnosis
    assert result == {"ok": False, "data": {"budget_expired": True, "run_dispatched": False, "source": "deadline"}}
    dispatch.assert_not_awaited()
