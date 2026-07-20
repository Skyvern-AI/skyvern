from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from structlog.testing import capture_logs

from skyvern.config import settings
from skyvern.forge.sdk.copilot import agent
from skyvern.forge.sdk.copilot.build_test_outcome import (
    RecordedBuildTestOutcome,
    RecordedOutcomeBindingConstraint,
    RecordedOutcomeGroundingRequirement,
)
from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy
from skyvern.forge.sdk.copilot.context import CodeAuthoringRepairContext, CopilotContext
from skyvern.forge.sdk.copilot.request_policy import CompletionCriterion, RequestPolicy
from skyvern.forge.sdk.copilot.runtime import (
    DEFINITION_CONTRACT_UNSATISFIED_GATE_ID,
    RECORDED_OUTCOME_GROUNDING_BINDER_CEILING_GATE_ID,
    SYNTHESIZED_PARAMETER_BINDING_AMBIGUOUS_GATE_ID,
    cache_copilot_author_time_gate_log_only_ids,
    copilot_author_time_gate_log_only_enabled,
    record_author_time_gate_ablation_event,
)
from skyvern.forge.sdk.copilot.tools import workflow_update as wu
from tests.unit.copilot_test_helpers import make_copilot_ctx

_UNREFERENCED_DEFINITION_YAML = """\
title: Submit reusable request
workflow_definition:
  parameters:
  - {parameter_type: workflow, key: account_name, workflow_parameter_type: string}
  blocks:
  - block_type: code
    label: submit_request
    parameter_keys: []
    code: |
      await page.locator("#submit").click()
"""


def _definition_ctx() -> CopilotContext:
    ctx = make_copilot_ctx()
    ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
    ctx.request_policy = RequestPolicy(
        completion_criteria=[
            CompletionCriterion(
                id="definition-1",
                outcome="The workflow uses account name as a reusable input.",
                level="definition",
                output_path="workflow.parameters",
            )
        ]
    )
    return ctx


def _stub_successful_update(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_process_workflow_yaml(**_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            workflow_definition=SimpleNamespace(blocks=[SimpleNamespace(label="submit_request")]),
            proxy_location=None,
        )

    async def fake_get_prior_workflow(_ctx: object) -> None:
        return None

    monkeypatch.setattr(wu, "_process_workflow_yaml", fake_process_workflow_yaml)
    monkeypatch.setattr(wu, "_get_prior_workflow", fake_get_prior_workflow)
    monkeypatch.setattr(wu, "composition_page_evidence_error", lambda *_args, **_kwargs: None)


@pytest.fixture(autouse=True)
def _cloud_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ENV", "prod")
    monkeypatch.setattr(settings, "WORKFLOW_COPILOT_AUTHOR_TIME_GATE_LOG_ONLY", False)


def test_oss_policy_filters_ineligible_ids_and_preserves_legacy_blanket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_copilot_ctx()
    with capture_logs() as logs:
        cache_copilot_author_time_gate_log_only_ids(
            ctx,
            frozenset({DEFINITION_CONTRACT_UNSATISFIED_GATE_ID, "credential_reference_presence"}),
        )

    assert ctx.author_time_gate_log_only_ids == frozenset({DEFINITION_CONTRACT_UNSATISFIED_GATE_ID})
    assert copilot_author_time_gate_log_only_enabled(ctx, DEFINITION_CONTRACT_UNSATISFIED_GATE_ID) is True
    assert copilot_author_time_gate_log_only_enabled(ctx, "credential_reference_presence") is False
    assert any(
        log["event"] == "copilot_gate_log_only_ineligible" and log["gate_id"] == "credential_reference_presence"
        for log in logs
    )

    monkeypatch.setattr(settings, "ENV", "local")
    monkeypatch.setattr(settings, "WORKFLOW_COPILOT_AUTHOR_TIME_GATE_LOG_ONLY", True)
    assert copilot_author_time_gate_log_only_enabled(ctx, "output_contract_actuation") is True
    assert copilot_author_time_gate_log_only_enabled(ctx, DEFINITION_CONTRACT_UNSATISFIED_GATE_ID) is True
    assert copilot_author_time_gate_log_only_enabled(ctx, "credential_reference_presence") is False


@pytest.mark.asyncio
async def test_turn_setup_resolves_once_and_gate_checks_use_cached_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    resolver = AsyncMock(return_value=frozenset({DEFINITION_CONTRACT_UNSATISFIED_GATE_ID}))
    monkeypatch.setattr(
        agent.app, "AGENT_FUNCTION", SimpleNamespace(resolve_copilot_author_time_gate_log_only_ids=resolver)
    )
    ctx = make_copilot_ctx(turn_id="turn-1")

    with capture_logs() as logs:
        await agent._cache_copilot_author_time_gate_log_only_ids(ctx)
    for _ in range(4):
        assert copilot_author_time_gate_log_only_enabled(ctx, DEFINITION_CONTRACT_UNSATISFIED_GATE_ID) is True

    resolver.assert_awaited_once_with(turn_id="turn-1", organization_id="org-1")
    assert [log for log in logs if log["event"] == "copilot_author_time_gate_log_only_registry_resolved"] == [
        {
            "event": "copilot_author_time_gate_log_only_registry_resolved",
            "log_level": "info",
            "selected_gate_ids": [DEFINITION_CONTRACT_UNSATISFIED_GATE_ID],
            "selected_gate_count": 1,
        }
    ]


def test_definition_gate_replay_records_one_event_per_suppression() -> None:
    ctx = make_copilot_ctx()
    cache_copilot_author_time_gate_log_only_ids(ctx, frozenset({DEFINITION_CONTRACT_UNSATISFIED_GATE_ID}))
    rejection = wu._DefinitionPlaneReject(
        criterion_ids=("criterion-1",),
        reason_codes=("definition_parameters_unreferenced",),
        unreferenced_parameter_keys=("account_name",),
    )
    workflow_yaml = _UNREFERENCED_DEFINITION_YAML

    for _ in range(16):
        assert wu._record_definition_plane_ablation_event(ctx, workflow_yaml, rejection) is True

    assert len(ctx.author_time_gate_ablation_events) == 16
    assert {event.gate_id for event in ctx.author_time_gate_ablation_events} == {
        DEFINITION_CONTRACT_UNSATISFIED_GATE_ID
    }
    assert len({event.fingerprint for event in ctx.author_time_gate_ablation_events}) == 1


@pytest.mark.asyncio
async def test_selected_definition_reject_persists_without_reject_state(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_successful_update(monkeypatch)
    ctx = _definition_ctx()
    cache_copilot_author_time_gate_log_only_ids(ctx, frozenset({DEFINITION_CONTRACT_UNSATISFIED_GATE_ID}))

    result = await wu._update_workflow(
        {"workflow_yaml": _UNREFERENCED_DEFINITION_YAML},
        ctx,
        allow_missing_credentials=True,
        allow_static_output_uncertainty=True,
    )

    assert result["ok"] is True
    assert ctx.latest_recorded_build_test_outcome is None
    assert ctx.blocker_signal is None
    assert ctx.turn_halt is None
    assert [event.gate_id for event in ctx.author_time_gate_ablation_events] == [
        DEFINITION_CONTRACT_UNSATISFIED_GATE_ID
    ]


def test_selected_definition_reject_strict_run_preflight_records_ablation_event() -> None:
    ctx = _definition_ctx()
    cache_copilot_author_time_gate_log_only_ids(ctx, frozenset({DEFINITION_CONTRACT_UNSATISFIED_GATE_ID}))

    result = wu._metadata_contract_run_preflight_reject(
        ctx,
        _UNREFERENCED_DEFINITION_YAML,
        [],
        enforce_untagged_declared_inputs=True,
    )

    assert result is None
    assert ctx.latest_recorded_build_test_outcome is None
    assert [event.gate_id for event in ctx.author_time_gate_ablation_events] == [
        DEFINITION_CONTRACT_UNSATISFIED_GATE_ID
    ]


def test_binder_ceiling_suppression_does_not_stash_reject_state(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = make_copilot_ctx()
    cache_copilot_author_time_gate_log_only_ids(ctx, frozenset({RECORDED_OUTCOME_GROUNDING_BINDER_CEILING_GATE_ID}))
    outcome = RecordedBuildTestOutcome(
        phase="author_time_reject",
        verdict="authoring_rejected",
        reason_code="definition_contract_unsatisfied",
        structural_failure_identity="definition-fingerprint",
    )
    assert outcome.structural_key is not None
    ctx.latest_recorded_build_test_outcome = outcome
    ctx.recorded_outcome_grounding_requirement = RecordedOutcomeGroundingRequirement(
        phase="author_time_reject",
        reason_code="definition_contract_unsatisfied",
        structural_key=outcome.structural_key,
        satisfied=True,
    )
    ctx.recorded_outcome_binding_constraint = RecordedOutcomeBindingConstraint(
        repeated_structural_key=outcome.structural_key,
        phase="author_time_reject",
        reason_code="definition_contract_unsatisfied",
        frontier_facet="value_shape",
    )
    monkeypatch.setattr(wu, "latest_recorded_build_test_outcome_repeated", lambda _ctx: True)

    assert wu._stash_unresolved_recorded_outcome_grounding_halt(ctx, ["account_name"]) is False
    assert ctx.turn_halt is None
    assert ctx.blocker_signal is None
    assert ctx.latest_recorded_build_test_outcome is outcome
    assert ctx.author_time_gate_ablation_events[-1].gate_id == RECORDED_OUTCOME_GROUNDING_BINDER_CEILING_GATE_ID


def test_synthesized_binding_suppression_records_bounded_structural_event() -> None:
    ctx = make_copilot_ctx()
    cache_copilot_author_time_gate_log_only_ids(ctx, frozenset({SYNTHESIZED_PARAMETER_BINDING_AMBIGUOUS_GATE_ID}))
    repair_context = CodeAuthoringRepairContext(
        block_label="submit_request",
        reason_code="synthesized_parameter_binding_ambiguous",
        unresolved_names=["account_name"],
        parameter_keys=["account_name"],
        available_parameter_keys=["customer_name"],
        binding_candidates=["account_name", "customer_name"],
    )

    assert (
        wu._record_synthesized_parameter_binding_ablation_event(
            ctx,
            _UNREFERENCED_DEFINITION_YAML,
            repair_context,
        )
        is True
    )
    event = ctx.author_time_gate_ablation_events[-1]
    assert event.gate_id == SYNTHESIZED_PARAMETER_BINDING_AMBIGUOUS_GATE_ID
    assert event.reason_code == "synthesized_parameter_binding_ambiguous"
    assert event.payload["unresolved_names"] == ["account_name"]


def test_structural_gate_suppression_fails_closed_without_fingerprint(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = make_copilot_ctx()
    cache_copilot_author_time_gate_log_only_ids(
        ctx,
        frozenset(
            {
                DEFINITION_CONTRACT_UNSATISFIED_GATE_ID,
                SYNTHESIZED_PARAMETER_BINDING_AMBIGUOUS_GATE_ID,
            }
        ),
    )
    rejection = wu._DefinitionPlaneReject(
        criterion_ids=("criterion-1",),
        reason_codes=("definition_parameters_unreferenced",),
        unreferenced_parameter_keys=("account_name",),
    )
    repair_context = CodeAuthoringRepairContext(
        block_label="submit_request",
        reason_code="synthesized_parameter_binding_ambiguous",
        unresolved_names=["account_name"],
        parameter_keys=["account_name"],
        available_parameter_keys=["customer_name"],
        binding_candidates=["account_name", "customer_name"],
    )
    monkeypatch.setattr(wu, "authored_structure_signature_from_workflow", lambda *_args: None)

    assert wu._record_definition_plane_ablation_event(ctx, "invalid", rejection) is False
    assert wu._record_synthesized_parameter_binding_ablation_event(ctx, "invalid", repair_context) is False
    assert ctx.author_time_gate_ablation_events == []


@pytest.mark.asyncio
async def test_selected_synthesized_binding_reject_persists_without_repair_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_successful_update(monkeypatch)
    ctx = make_copilot_ctx()
    cache_copilot_author_time_gate_log_only_ids(ctx, frozenset({SYNTHESIZED_PARAMETER_BINDING_AMBIGUOUS_GATE_ID}))
    repair_context = CodeAuthoringRepairContext(
        block_label="submit_request",
        reason_code="synthesized_parameter_binding_ambiguous",
        unresolved_names=["account_name"],
        parameter_keys=["account_name"],
        available_parameter_keys=["customer_name"],
        binding_candidates=["account_name", "customer_name"],
    )
    monkeypatch.setattr(
        wu,
        "_maybe_impose_synthesized_code_block",
        lambda *_args, **_kwargs: wu._SynthesizedCodeImpositionResult(
            workflow_yaml=_UNREFERENCED_DEFINITION_YAML,
            violations=["Unable to bind synthesized parameter."],
            repair_context=repair_context,
            ablation_gate_id=SYNTHESIZED_PARAMETER_BINDING_AMBIGUOUS_GATE_ID,
        ),
    )

    result = await wu._update_workflow(
        {"workflow_yaml": _UNREFERENCED_DEFINITION_YAML},
        ctx,
        allow_missing_credentials=True,
        allow_static_output_uncertainty=True,
    )

    assert result["ok"] is True
    assert ctx.latest_recorded_build_test_outcome is None
    assert ctx.last_code_authoring_repair_context is None
    assert ctx.author_time_gate_ablation_events[-1].gate_id == SYNTHESIZED_PARAMETER_BINDING_AMBIGUOUS_GATE_ID


def test_unselected_new_gate_keeps_enforcing() -> None:
    ctx = make_copilot_ctx()
    assert (
        record_author_time_gate_ablation_event(
            ctx,
            gate_id=DEFINITION_CONTRACT_UNSATISFIED_GATE_ID,
            reason_code="definition_contract_unsatisfied",
            fingerprint="fingerprint",
        )
        is False
    )
    assert ctx.author_time_gate_ablation_events == []
