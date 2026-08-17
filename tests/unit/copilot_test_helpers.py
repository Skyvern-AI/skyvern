"""Shared builders for copilot unit tests."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.copilot.build_test_outcome import RecordedBuildTestOutcome
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
from skyvern.forge.sdk.copilot.tools import run_execution as run_execution_module

DISPATCHED_LOGIN_GATE_HTML = (
    "<html><head><title>Sign in</title></head><body><main>"
    "<h1>Sign in to continue</h1>"
    '<form id="signin" action="/session" method="post">'
    '<label for="account-email">Email</label>'
    '<input id="account-email" name="email" type="email" required />'
    '<label for="account-password">Password</label>'
    '<input id="account-password" name="password" type="password" required />'
    '<button type="submit">Sign in</button>'
    "</form></main></body></html>"
)
DISPATCHED_RESULTS_HTML = (
    "<html><head><title>Available providers</title></head><body><main>"
    "<h1>Available providers</h1>"
    '<table id="provider-results"><tbody>'
    "<tr><td>Example Fiber</td><td>up to 500 Mbps</td></tr>"
    "<tr><td>Example Cable</td><td>up to 300 Mbps</td></tr>"
    "</tbody></table></main></body></html>"
)
DISPATCHED_NAV_ONLY_HTML = (
    "<html><head><title>Site map</title></head><body><main>"
    '<a href="https://example.test/plans">Plans</a>'
    '<a href="https://example.test/support">Support</a>'
    "</main></body></html>"
)


def make_stub_artifact(
    artifact_id: str,
    file_name: str,
    file_size: int | None,
    artifact_type: ArtifactType = ArtifactType.DOWNLOAD,
) -> SimpleNamespace:
    return SimpleNamespace(
        artifact_id=artifact_id,
        uri=f"s3://bucket/{file_name}",
        file_size=file_size,
        artifact_type=artifact_type,
    )


def make_stub_html_artifact(
    artifact_id: str,
    artifact_type: ArtifactType,
    file_size: int | None = 400,
    created_at: datetime | None = None,
) -> SimpleNamespace:
    artifact = make_stub_artifact(artifact_id, f"{artifact_id}.html", file_size, artifact_type=artifact_type)
    artifact.created_at = created_at or datetime(2026, 7, 9, tzinfo=timezone.utc)
    return artifact


def stub_artifact_app(
    monkeypatch: pytest.MonkeyPatch,
    artifacts: list[SimpleNamespace],
    retrieved: dict[str, bytes],
    *,
    by_ids: list[SimpleNamespace] | None = None,
) -> list[str]:
    retrieved_ids: list[str] = []

    async def fake_get_artifacts_for_run(
        run_id: str, *, organization_id: str, artifact_types: object
    ) -> list[SimpleNamespace]:
        return artifacts

    async def fake_get_artifacts_by_ids(artifact_ids: list[str], *, organization_id: str) -> list[SimpleNamespace]:
        pool = {artifact.artifact_id: artifact for artifact in (by_ids if by_ids is not None else artifacts)}
        return [pool[artifact_id] for artifact_id in artifact_ids if artifact_id in pool]

    async def fake_retrieve_artifact(artifact: SimpleNamespace) -> bytes:
        retrieved_ids.append(artifact.artifact_id)
        return retrieved.get(artifact.artifact_id, b"")

    fake_app = SimpleNamespace(
        DATABASE=SimpleNamespace(
            artifacts=SimpleNamespace(
                get_artifacts_for_run=fake_get_artifacts_for_run,
                get_artifacts_by_ids=fake_get_artifacts_by_ids,
            )
        ),
        ARTIFACT_MANAGER=SimpleNamespace(retrieve_artifact=fake_retrieve_artifact),
    )
    monkeypatch.setattr(run_execution_module, "app", fake_app)
    return retrieved_ids


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
    antecedent_family: str | None = None,
    kind: str = "outcome",
    terminal_action_family: str | None = None,
    deliverable_kind: str | None = None,
    deliverable_confirmation_criterion_id: str | None = None,
    expected_output_value: str | None = None,
    expected_output_shape: str | None = None,
    requested_output_evidence_source: str = "runtime_output",
    requested_output_path_mint_source: str | None = None,
    classification_output_key: str | None = None,
    expected_classification: str | bool | None = None,
    requested_output_corroborator: bool = False,
    mint_degrade: str | None = None,
    requested_output_floor_rekeyed: bool = False,
    floor_rekeyed_from_path: str | None = None,
) -> CompletionCriterion:
    return CompletionCriterion(
        id=cid,
        outcome=outcome,
        level=level,  # type: ignore[arg-type]
        method_mandated=method_mandated,
        output_path=output_path,
        contingent_on=contingent_on,
        contingent_antecedent_output_path=contingent_antecedent_output_path,
        antecedent_family=antecedent_family,  # type: ignore[arg-type]
        kind=kind,  # type: ignore[arg-type]
        terminal_action_family=terminal_action_family,  # type: ignore[arg-type]
        deliverable_kind=deliverable_kind,  # type: ignore[arg-type]
        deliverable_confirmation_criterion_id=deliverable_confirmation_criterion_id,
        expected_output_value=expected_output_value,
        expected_output_shape=expected_output_shape,  # type: ignore[arg-type]
        requested_output_evidence_source=requested_output_evidence_source,  # type: ignore[arg-type]
        requested_output_path_mint_source=requested_output_path_mint_source,  # type: ignore[arg-type]
        classification_output_key=classification_output_key,
        expected_classification=expected_classification,
        requested_output_corroborator=requested_output_corroborator,
        mint_degrade=mint_degrade,  # type: ignore[arg-type]
        requested_output_floor_rekeyed=requested_output_floor_rekeyed,
        floor_rekeyed_from_path=floor_rekeyed_from_path,
    )


def two_page_login_yaml(*, submit_selector: str = "Login") -> str:
    """The shape copilot emits in code-block mode: branch bodies are code inside one always-executed
    block, so a passing run can traverse it without reaching the guarded call."""
    return f"""
    title: Sign in and read the metric
    workflow_definition:
      blocks:
      - block_type: code
        label: sign_in_and_read
        code: |
          await page.fill("#user", "demo")
          await page.click("#submit")
          if await page.locator("#token").count():
              await page.get_by_role("button", name="{submit_selector}", exact=True).click()
          return {{"visitors": "9.42K"}}
    """


def straight_line_login_yaml() -> str:
    """One always-executed code block with no branching: executing it reaches every call in it."""
    return """
    title: Sign in and read the metric
    workflow_definition:
      blocks:
      - block_type: code
        label: sign_in_and_read
        code: |
          await page.fill("#user", "demo")
          await page.get_by_role("button", name="Login", exact=True).click()
          return {"visitors": "9.42K"}
    """


def failed_second_factor_run(run_id: str) -> RecordedBuildTestOutcome:
    return RecordedBuildTestOutcome(
        phase="persisted_block_run",
        attempted_tool="update_and_run_blocks",
        attempted_block_label="sign_in_and_read",
        attempted_call_ref="role:button:Login",
        verdict="repairable_failure",
        reason_code="runtime_block_failure",
        workflow_run_id=run_id,
        block_labels=["sign_in_and_read"],
        structural_failure_identity="locator-timeout-identity",
    )


def passing_run(run_id: str, block_labels: list[str]) -> RecordedBuildTestOutcome:
    return RecordedBuildTestOutcome(
        phase="persisted_block_run",
        attempted_tool="update_and_run_blocks",
        verdict="progress_observed",
        reason_code="run_completed_unevaluated",
        workflow_run_id=run_id,
        block_labels=block_labels,
        structural_failure_identity="",
        evidence_refs=["rows:1"],
    )


InteractionFieldValue = str | int | bool | None | list[Any] | dict[str, Any]


def carried_interaction(**fields: InteractionFieldValue) -> dict[str, Any]:
    """One entry of the cross-turn carried trajectory.

    The record is plain interaction dicts, so this only spares tests the brace noise.
    """
    return dict(fields)
