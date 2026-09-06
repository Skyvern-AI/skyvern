"""Shared builders for copilot unit tests."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.forge import app as forge_app
from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.copilot.active_run_session import ActiveRunSessionAssociation
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
from skyvern.forge.sdk.copilot.runtime import record_sensitive_origin_run_taint, register_sensitive_origin_run_lease
from skyvern.forge.sdk.copilot.tools import run_execution as run_execution_module
from skyvern.forge.sdk.copilot.workflow_yaml import _process_workflow_yaml as process_workflow_yaml
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.schemas.workflow_runs import WorkflowRunBlock
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter, WorkflowParameter
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus
from skyvern.schemas.workflows import BlockType
from skyvern.services import workflow_service as workflow_service_module

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


def _fake_workflow_run(status: str) -> SimpleNamespace:
    return SimpleNamespace(
        status=WorkflowRunStatus(status),
        modified_at=datetime(2026, 4, 21, 12, 0, 0, tzinfo=timezone.utc),
        browser_session_id=None,
        failure_reason=None,
    )


async def install_run_blocks_harness(
    monkeypatch: pytest.MonkeyPatch,
    *,
    workflow_yaml: str,
    polled_status: str,
    dispatch_to_worker: bool = False,
    terminal_blocks: list[WorkflowRunBlock] | None = None,
) -> dict[str, Any]:
    """Stub the collaborators an inline ``_run_blocks_and_collect_debug`` call reaches, with the
    polled run parked on ``polled_status`` so the watchdog decides the exit."""
    monkeypatch.setattr(forge_app.WORKFLOW_SERVICE, "get_workflow_by_permanent_id", AsyncMock(return_value=None))
    workflow = await process_workflow_yaml(
        settings_fallback_yaml="enable_self_healing: false",
        workflow_id="w_source",
        workflow_permanent_id="wfp-1",
        organization_id="org-1",
        workflow_yaml=workflow_yaml,
    )
    now = datetime.now(timezone.utc)
    organization = Organization(
        organization_id="org-1",
        organization_name="Test Org",
        created_at=now,
        modified_at=now,
    )
    captured: dict[str, Any] = {"workflow": workflow, "executor_cancelled": False}

    database = MagicMock()
    database.workflows.get_workflow_by_permanent_id = AsyncMock(return_value=workflow)
    database.workflows.get_workflow = AsyncMock(return_value=workflow)
    database.organizations.get_organization = AsyncMock(return_value=organization)
    persisted_output_params = [p for p in workflow.workflow_definition.parameters if isinstance(p, OutputParameter)]
    persisted_workflow_params = [p for p in workflow.workflow_definition.parameters if isinstance(p, WorkflowParameter)]
    database.workflow_params.get_workflow_output_parameters = AsyncMock(return_value=persisted_output_params)
    database.observer.get_workflow_run_blocks = AsyncMock(return_value=terminal_blocks or [])
    database.workflow_runs.get_workflow_run = AsyncMock(return_value=_fake_workflow_run(status=polled_status))
    monkeypatch.setattr(forge_app, "DATABASE", database)

    async def _execute_workflow(**_kwargs: Any) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            captured["executor_cancelled"] = True
            raise

    workflow_service = MagicMock()
    workflow_service.get_workflow_parameters = AsyncMock(return_value=persisted_workflow_params)
    workflow_service.execute_workflow = AsyncMock(side_effect=_execute_workflow)
    workflow_service.create_copilot_dispatch_draft_version = AsyncMock(return_value=workflow)
    monkeypatch.setattr(forge_app, "WORKFLOW_SERVICE", workflow_service)
    monkeypatch.setattr(
        forge_app.AGENT_FUNCTION,
        "should_dispatch_copilot_block_run_to_worker",
        AsyncMock(return_value=dispatch_to_worker),
    )
    monkeypatch.setattr(
        forge_app.AGENT_FUNCTION,
        "allow_copilot_inline_code_execution",
        MagicMock(return_value=False),
    )

    workflow_run = SimpleNamespace(
        workflow_run_id="wr_paused",
        workflow_id="w_source",
        sequential_credential_id=None,
    )
    monkeypatch.setattr(workflow_service_module, "prepare_workflow", AsyncMock(return_value=workflow_run))

    polled_run = _fake_workflow_run(status=polled_status)

    async def _read_progress(_ctx: CopilotContext, _run_id: str) -> tuple[Any, Any, Any]:
        return polled_run, now, now

    monkeypatch.setattr(run_execution_module, "_read_progress_sources", _read_progress)
    monkeypatch.setattr(run_execution_module, "RUN_BLOCKS_POLL_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(run_execution_module, "_fallback_page_info", AsyncMock(return_value=("", "")))

    association = ActiveRunSessionAssociation(
        organization_id="org-1",
        workflow_permanent_id="wfp-1",
        debug_browser_session_id="pbs_chat",
        run_browser_session_id="pbs_run",
        workflow_run_id="wr_paused",
        turn_id="turn-1",
        generation="gen-1",
        expires_at=now + timedelta(minutes=5),
    )
    captured["publish"] = AsyncMock(return_value=association)
    captured["clear"] = AsyncMock(return_value=True)
    captured["cancel_run_task"] = AsyncMock(return_value=None)
    captured["cooperative_cancel"] = AsyncMock(return_value=None)
    monkeypatch.setattr(run_execution_module, "publish_active_run_session", captured["publish"])
    monkeypatch.setattr(run_execution_module, "clear_active_run_session", captured["clear"])
    monkeypatch.setattr(run_execution_module, "_cancel_run_task_if_not_final", captured["cancel_run_task"])
    monkeypatch.setattr(run_execution_module, "_cooperative_cancel_dispatched_run", captured["cooperative_cancel"])
    if dispatch_to_worker:
        captured["worker_execute"] = AsyncMock(return_value=None)
        monkeypatch.setattr(
            run_execution_module.AsyncExecutorFactory,
            "get_executor",
            MagicMock(return_value=SimpleNamespace(execute_workflow=captured["worker_execute"])),
        )
        monkeypatch.setattr(run_execution_module, "_delete_dispatch_draft_if_run_final", AsyncMock(return_value=None))
        monkeypatch.setattr(
            run_execution_module, "_capture_dispatched_terminal_page_evidence", AsyncMock(return_value=None)
        )
        monkeypatch.setattr(
            run_execution_module, "_attach_registered_output_parameter_values", AsyncMock(return_value={})
        )
    return captured


HANDBACK_WORKFLOW_YAML = """
title: extraction example
workflow_definition:
  parameters: []
  blocks:
    - block_type: extraction
      label: extract_heading
      url: https://example.com
      data_extraction_goal: Extract the page heading.
"""


def terminal_extraction_block(status: str) -> WorkflowRunBlock:
    return WorkflowRunBlock(
        label="extract_heading",
        block_type=BlockType.EXTRACTION,
        status=status,
        failure_reason=(
            'Timeout exceeded: waiting for locator("#heading") to be visible' if status == "failed" else None
        ),
        workflow_run_block_id="wrb_extract_heading",
        workflow_run_id="wr_paused",
        organization_id="org-1",
        created_at=datetime(2026, 4, 21, 12, 5, tzinfo=UTC),
        modified_at=datetime(2026, 4, 21, 12, 5, tzinfo=UTC),
    )


def page_only_failed_block() -> WorkflowRunBlock:
    """A failed block with no failure_reason, so the post-run page is its only structural signal."""
    return WorkflowRunBlock(
        label="extract_heading",
        block_type=BlockType.EXTRACTION,
        status="failed",
        failure_reason=None,
        workflow_run_block_id="wrb_extract_heading",
        workflow_run_id="wr_paused",
        organization_id="org-1",
        created_at=datetime(2026, 4, 21, 12, 5, tzinfo=UTC),
        modified_at=datetime(2026, 4, 21, 12, 5, tzinfo=UTC),
    )


def same_run_page_evidence() -> dict[str, object]:
    return {
        "observed_after_workflow_run": True,
        "workflow_run_id": "wr_paused",
        "source_browser_session_id": "pbs_run",
        "current_url": "https://example.com/done",
        "page_title": "Done",
        "inspected_url": "https://example.com/done",
    }


def count_record_and_send(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    counts = {"record": 0, "send": 0}
    real_record = run_execution_module.record_build_test_outcome
    real_send = run_execution_module._send_run_outcome_update

    def _record(ctx: object, outcome: object) -> None:
        counts["record"] += 1
        real_record(ctx, outcome)

    async def _send(*args: object, **kwargs: object) -> None:
        counts["send"] += 1
        await real_send(*args, **kwargs)

    monkeypatch.setattr(run_execution_module, "record_build_test_outcome", _record)
    monkeypatch.setattr(run_execution_module, "_send_run_outcome_update", _send)
    return counts


async def handback_ctx(
    monkeypatch: pytest.MonkeyPatch,
    *,
    polled_status: str,
    block_status: str,
    terminal_blocks: list[WorkflowRunBlock] | None = None,
) -> CopilotContext:
    harness = await install_run_blocks_harness(
        monkeypatch,
        workflow_yaml=HANDBACK_WORKFLOW_YAML,
        polled_status=polled_status,
        terminal_blocks=terminal_blocks or [terminal_extraction_block(block_status)],
    )
    ctx = make_copilot_ctx(browser_session_id="pbs_chat")
    ctx.staged_workflow = harness["workflow"]
    ctx.frontier_resume_session_id = "pbs_run"
    return ctx


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


def make_model_input_data(items: list[Any], *, instructions: str | None = None, context: Any = None) -> Any:
    """Build a fake CallModelData payload with a model_data.input list.

    ``CallModelData.context`` is the run context itself (``TContext | None``), not a wrapper around
    one; a fake that nests it hides an attribute error behind a passing test.
    """
    return SimpleNamespace(
        model_data=SimpleNamespace(input=list(items), instructions=instructions),
        context=context,
    )


class FakeMCPServerManager:
    def __init__(self, servers: object) -> None:
        self.active_servers = servers

    async def __aenter__(self) -> FakeMCPServerManager:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


def stub_copilot_agent_loop(
    monkeypatch: pytest.MonkeyPatch, run_with_enforcement: Callable[..., Awaitable[object]]
) -> None:
    def fake_resolve_model_config(
        _handler: object, *, copilot_config: object = None, llm_key_override: str | None = None
    ) -> tuple[str, object, str, bool]:
        return f"model-{llm_key_override or 'PRIMARY'}", object(), llm_key_override or "PRIMARY", True

    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.agent._resolve_live_browser_session_id", AsyncMock(return_value=None)
    )
    monkeypatch.setattr("agents.mcp.MCPServerManager", FakeMCPServerManager)
    monkeypatch.setattr("skyvern.forge.sdk.copilot.model_resolver.resolve_model_config", fake_resolve_model_config)
    monkeypatch.setattr("skyvern.forge.sdk.copilot.enforcement.run_with_enforcement", run_with_enforcement)


SENSITIVE_DISCLOSURE_WITHHOLDING_ARMS = [
    "registry_missing",
    "registry_incomplete",
    "registry_other_run",
    "run_still_active",
    "run_id_unclaimed",
    "second_browser_run_replaced_registry",
    "same_session_earlier_run_unbound",
]


def taint_by_terminal_run(ctx: Any, *, workflow_run_id: str, session_id: str) -> None:
    """Mark ``session_id`` as the page a finished credential run left, attributed to that run."""
    record_sensitive_origin_run_taint(ctx, workflow_run_id=workflow_run_id, session_id=session_id)


def remove_sensitive_disclosure_prerequisite(ctx: Any, arm: str) -> None:
    """Drop exactly one prerequisite of the terminal-matching-registry disclosure route."""
    registry = ctx.origin_run_redaction_registry
    if arm == "registry_missing":
        ctx.origin_run_redaction_registry = None
    elif arm == "registry_incomplete":
        ctx.origin_run_redaction_registry = replace(registry, contains_all_sensitive_values=False)
    elif arm == "registry_other_run":
        ctx.origin_run_redaction_registry = replace(registry, workflow_run_id="wr_unrelated")
    elif arm == "run_still_active":
        register_sensitive_origin_run_lease(
            ctx, workflow_run_id=registry.workflow_run_id, session_id=ctx.browser_session_id
        )
    elif arm == "run_id_unclaimed":
        ctx.last_run_blocks_workflow_run_id = None
    elif arm == "second_browser_run_replaced_registry":
        # A later run on another browser finished with a complete registry and is the run the
        # model now claims; the page under inspection was tainted by the earlier run, whose
        # values were never bound. The complete registry must not unlock that page.
        record_sensitive_origin_run_taint(ctx, workflow_run_id="wr_second", session_id="pbs_second_browser")
        ctx.last_run_blocks_workflow_run_id = "wr_second"
        ctx.origin_run_redaction_registry = replace(
            registry, workflow_run_id="wr_second", contains_all_sensitive_values=True
        )
    elif arm == "same_session_earlier_run_unbound":
        # An earlier run on this same page ended without completing its registry, so a value
        # it entered may be on the page while the claimed run's complete registry knows nothing of it.
        record_sensitive_origin_run_taint(ctx, workflow_run_id="wr_earlier", session_id=ctx.browser_session_id)
    else:
        raise AssertionError(f"unknown arm {arm}")
