"""Shared builders for copilot unit tests (CopilotContext + verified-goal contract)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.diagnosis_repair_contract import (
    DiagnosisInput,
    DiagnosisRepairContract,
    DiagnosisResult,
    RepairDecision,
    RepairNextAction,
    VerificationResult,
)
from skyvern.forge.sdk.copilot.request_policy import CompletionCriterion


def make_copilot_ctx(**overrides: object) -> CopilotContext:
    defaults: dict[str, object] = dict(
        organization_id="org-1",
        workflow_id="wf-1",
        workflow_permanent_id="wfp-1",
        workflow_yaml="",
        browser_session_id=None,
        stream=MagicMock(),
    )
    defaults.update(overrides)
    return CopilotContext(**defaults)


def make_raw_loaded_result_context(
    *,
    include_sample_rows: bool = False,
    include_text: bool = False,
    include_user_goal: bool = False,
) -> str:
    target: dict[str, object] = {
        "selector": '#account-123456-JaneCustomer-results[data-customer="Jane Customer"]',
        "is_table": True,
        "row_selector": 'tr[data-account="987654321"]',
        "row_count": 2,
    }
    if include_sample_rows:
        target["sample_rows"] = ["Jane Customer account 123456"]
    if include_text:
        target["text"] = "Jane Customer statement results"
    target["structure_signature"] = "legacy-selector-derived-sig"
    payload: dict[str, object] = {}
    if include_user_goal:
        payload["user_goal"] = "extract loaded results"
    payload["loaded_result_targets"] = [target]
    return json.dumps(payload)


def make_verified_goal_contract(
    *, next_action: RepairNextAction = RepairNextAction.NO_CHANGE
) -> DiagnosisRepairContract:
    return DiagnosisRepairContract(
        diagnosis_input=DiagnosisInput(source_tool="update_and_run_blocks"),
        diagnosis_result=DiagnosisResult(),
        repair_decision=RepairDecision(next_action=next_action),
        verification_result=VerificationResult(
            user_goal_satisfied=True,
            completion_contract_satisfied=True,
        ),
    )


def make_completion_criterion(
    cid: str,
    outcome: str,
    *,
    level: str = "run",
    method_mandated: bool = False,
    output_path: str | None = None,
    contingent_on: str | None = None,
    contingent_antecedent_output_path: str | None = None,
    kind: str = "outcome",
    terminal_action_family: str | None = None,
    deliverable_kind: str | None = None,
    expected_output_value: str | None = None,
    expected_output_shape: str | None = None,
    requested_output_evidence_source: str = "runtime_output",
    classification_output_key: str | None = None,
    expected_classification: str | bool | None = None,
    requested_output_corroborator: bool = False,
) -> CompletionCriterion:
    return CompletionCriterion(
        id=cid,
        outcome=outcome,
        level=level,  # type: ignore[arg-type]
        method_mandated=method_mandated,
        output_path=output_path,
        contingent_on=contingent_on,
        contingent_antecedent_output_path=contingent_antecedent_output_path,
        kind=kind,  # type: ignore[arg-type]
        terminal_action_family=terminal_action_family,  # type: ignore[arg-type]
        deliverable_kind=deliverable_kind,  # type: ignore[arg-type]
        expected_output_value=expected_output_value,
        expected_output_shape=expected_output_shape,  # type: ignore[arg-type]
        requested_output_evidence_source=requested_output_evidence_source,  # type: ignore[arg-type]
        classification_output_key=classification_output_key,
        expected_classification=expected_classification,
        requested_output_corroborator=requested_output_corroborator,
    )
