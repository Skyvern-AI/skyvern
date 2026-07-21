from __future__ import annotations

import copy
import json
import pathlib
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from structlog.testing import capture_logs

from skyvern.config import settings
from skyvern.forge.sdk.copilot import agent, runtime
from skyvern.forge.sdk.copilot.build_test_outcome import (
    RecordedBuildTestOutcome,
    RecordedOutcomeBindingConstraint,
    RecordedOutcomeGroundingRequirement,
)
from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy
from skyvern.forge.sdk.copilot.context import CodeAuthoringRepairContext, CopilotContext
from skyvern.forge.sdk.copilot.request_policy import CompletionCriterion, RequestPolicy
from skyvern.forge.sdk.copilot.runtime import (
    AUTHOR_TIME_GATE_LOG_ONLY_IDS,
    DEFINITION_CONTRACT_UNSATISFIED_GATE_ID,
    METADATA_RUN_PREFLIGHT_REJECT_GATE_ID,
    OUTPUT_CONTRACT_ACTUATION_GATE_ID,
    RECORDED_OUTCOME_GROUNDING_BINDER_CEILING_GATE_ID,
    SYNTHESIZED_PARAMETER_BINDING_AMBIGUOUS_GATE_ID,
    cache_copilot_author_time_gate_log_only_ids,
    copilot_author_time_gate_log_only_enabled,
    record_author_time_gate_ablation_event,
)
from skyvern.forge.sdk.copilot.tools import workflow_update as wu
from tests.unit.copilot_test_helpers import make_copilot_ctx
from tests.unit.test_copilot_code_artifact_metadata_violations import _valid_metadata

_GOAL_PATH_PLACEHOLDER = "<fill: output JSON path(s) carrying requested goal values>"

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


_MISSING_METADATA_OUTPUT_YAML = """\
title: Collect provider records
workflow_definition:
  parameters:
  - {parameter_type: output, key: records}
  blocks:
  - block_type: code
    label: collect_records
    parameter_keys: []
    code: |
      await page.goto("https://example.com/records")
      return {"records": [{"number": "123"}]}
"""


_IMPOSABLE_METADATA = {
    "block_label": "collect_records",
    "declared_goal": "Collect the provider records",
    "terminal_verifier_expectations": [{"goal_value_paths": ["records[].number"]}],
}


def _missing_metadata_ctx() -> CopilotContext:
    ctx = make_copilot_ctx()
    ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
    return ctx


def test_metadata_family_gate_ids_are_posthog_eligible() -> None:
    ctx = make_copilot_ctx()
    with capture_logs() as logs:
        cache_copilot_author_time_gate_log_only_ids(
            ctx,
            frozenset(
                {
                    OUTPUT_CONTRACT_ACTUATION_GATE_ID,
                    METADATA_RUN_PREFLIGHT_REJECT_GATE_ID,
                    "raw_secret_leak",
                    "credential_reference_presence",
                }
            ),
        )

    assert ctx.author_time_gate_log_only_ids == frozenset(
        {OUTPUT_CONTRACT_ACTUATION_GATE_ID, METADATA_RUN_PREFLIGHT_REJECT_GATE_ID}
    )
    assert copilot_author_time_gate_log_only_enabled(ctx, OUTPUT_CONTRACT_ACTUATION_GATE_ID) is True
    assert copilot_author_time_gate_log_only_enabled(ctx, METADATA_RUN_PREFLIGHT_REJECT_GATE_ID) is True
    ineligible = {log["gate_id"] for log in logs if log["event"] == "copilot_gate_log_only_ineligible"}
    assert ineligible == {"raw_secret_leak", "credential_reference_presence"}
    for security_gate_id in ("raw_secret_leak", "credential_reference_presence"):
        assert copilot_author_time_gate_log_only_enabled(ctx, security_gate_id) is False


@pytest.mark.asyncio
async def test_missing_metadata_reject_persists_draft_under_log_only(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_successful_update(monkeypatch)
    ctx = _missing_metadata_ctx()
    cache_copilot_author_time_gate_log_only_ids(ctx, frozenset({OUTPUT_CONTRACT_ACTUATION_GATE_ID}))

    result = await wu._update_workflow(
        {"workflow_yaml": _MISSING_METADATA_OUTPUT_YAML},
        ctx,
        allow_missing_credentials=True,
        allow_static_output_uncertainty=True,
    )

    assert result["ok"] is True
    assert ctx.latest_recorded_build_test_outcome is None
    event = ctx.author_time_gate_ablation_events[-1]
    assert event.gate_id == OUTPUT_CONTRACT_ACTUATION_GATE_ID
    assert event.reason_code == "missing_code_artifact_metadata"
    assert event.log_only is True
    assert event.blocked_tool == "update_workflow"
    assert event.fingerprint
    assert event.payload["block_labels"] == ["collect_records"]
    # No request-policy output contract is in play, so there is nothing to scaffold from and the
    # draft persists carrying no metadata rows.
    assert not ctx.code_artifact_metadata


@pytest.mark.asyncio
async def test_missing_metadata_reject_stays_enforcing_when_gate_unselected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_successful_update(monkeypatch)
    ctx = _missing_metadata_ctx()

    result = await wu._update_workflow(
        {"workflow_yaml": _MISSING_METADATA_OUTPUT_YAML},
        ctx,
        allow_missing_credentials=True,
        allow_static_output_uncertainty=True,
    )

    assert result["ok"] is False
    assert "code_artifact_metadata" in str(result["error"])
    assert ctx.author_time_gate_ablation_events == []
    outcome = ctx.latest_recorded_build_test_outcome
    assert outcome is not None
    assert outcome.reason_code == "metadata_reject"


_NORMALIZATION_YAML = """\
title: Collect provider records
workflow_definition:
  parameters: []
  blocks:
  - block_type: code
    label: collect_records
    parameter_keys: []
    code: |
      await page.goto("https://example.com/records")
      return {"records": [{"number": "123"}]}
"""


_TWO_BLOCK_YAML = """\
title: Collect provider records
workflow_definition:
  parameters: []
  blocks:
  - block_type: code
    label: block_ok
    parameter_keys: []
    code: |
      await page.goto("https://example.com/a")
      return {"records": [{"number": "1"}]}
  - block_type: code
    label: block_bad
    parameter_keys: []
    code: |
      await page.goto("https://example.com/b")
      return {"records": [{"number": "2"}]}
"""


@pytest.mark.asyncio
async def test_normalization_seam_keeps_rows_the_enforcing_pass_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_successful_update(monkeypatch)
    ctx = make_copilot_ctx()
    cache_copilot_author_time_gate_log_only_ids(ctx, frozenset({OUTPUT_CONTRACT_ACTUATION_GATE_ID}))
    conforming = copy.deepcopy(_valid_metadata("block_ok"))
    conforming["claimed_outcomes"][0]["goal_value_paths"] = [_GOAL_PATH_PLACEHOLDER]

    result = await wu._update_workflow(
        {
            "workflow_yaml": _TWO_BLOCK_YAML,
            "code_artifact_metadata": [conforming, {"block_label": "block_bad", "declared_goal": "g"}],
        },
        ctx,
        allow_missing_credentials=True,
        allow_static_output_uncertainty=True,
    )

    assert result["ok"] is True
    assert ctx.latest_recorded_build_test_outcome is None
    assert ctx.author_time_gate_ablation_events[-1].reason_code == "metadata_normalization"
    # block_ok normalizes on the enforcing pass; the seam must not drop it while relieving block_bad.
    assert set(ctx.code_artifact_metadata or {}) == {"block_ok"}


@pytest.mark.asyncio
async def test_schema_incompatibility_still_rejects_and_records_no_ablation_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_successful_update(monkeypatch)
    ctx = make_copilot_ctx()
    cache_copilot_author_time_gate_log_only_ids(ctx, frozenset({OUTPUT_CONTRACT_ACTUATION_GATE_ID}))
    incompatible = copy.deepcopy(_valid_metadata("collect_records"))
    incompatible["terminal_verifier_expectations"] = [
        {
            "goal_value_paths": ["records[].number"],
            "extraction_schema": json.dumps(
                {"type": "object", "properties": {"unrelated": {"type": "string"}}, "required": ["unrelated"]}
            ),
        }
    ]

    result = await wu._update_workflow(
        {"workflow_yaml": _NORMALIZATION_YAML, "code_artifact_metadata": [incompatible]},
        ctx,
        allow_missing_credentials=True,
        allow_static_output_uncertainty=True,
    )

    if result["ok"] is False and ctx.author_time_gate_ablation_events:
        raise AssertionError("a rejected save must not also report a log-only ablation for the same call")


@pytest.mark.asyncio
async def test_undefaultable_normalization_contradiction_saves_no_row_under_log_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_successful_update(monkeypatch)
    ctx = make_copilot_ctx()
    cache_copilot_author_time_gate_log_only_ids(ctx, frozenset({OUTPUT_CONTRACT_ACTUATION_GATE_ID}))

    result = await wu._update_workflow(
        {
            "workflow_yaml": _NORMALIZATION_YAML,
            "code_artifact_metadata": [
                {
                    "block_label": "collect_records",
                    "declared_goal": "collect records",
                    "claimed_outcomes": [{"id": "claim:x", "scope": "outcome", "text": "x", "status": "satisfied"}],
                }
            ],
        },
        ctx,
        allow_missing_credentials=True,
        allow_static_output_uncertainty=True,
    )

    assert result["ok"] is True
    assert ctx.author_time_gate_ablation_events[-1].reason_code == "metadata_normalization"
    assert not ctx.code_artifact_metadata


@pytest.mark.asyncio
async def test_normalization_reject_stays_enforcing_when_gate_unselected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_successful_update(monkeypatch)
    ctx = make_copilot_ctx()

    result = await wu._update_workflow(
        {
            "workflow_yaml": _NORMALIZATION_YAML,
            "code_artifact_metadata": [_IMPOSABLE_METADATA],
        },
        ctx,
        allow_missing_credentials=True,
        allow_static_output_uncertainty=True,
    )

    assert result["ok"] is False
    assert ctx.author_time_gate_ablation_events == []
    outcome = ctx.latest_recorded_build_test_outcome
    assert outcome is not None
    assert outcome.reason_code == "metadata_reject"


def test_eligible_gate_set_matches_the_seamed_gates_exactly() -> None:
    """Gate ids reaching an ablation recorder are the ground truth for what can be disabled:
    the flag's list must cover all of them, and every listed id must emit ablation events."""
    recorders = r"(?:record_author_time_gate_ablation_event|_record_output_contract_ablation_event)"
    seamed: set[str] = set()
    for path in pathlib.Path("skyvern/forge/sdk/copilot").rglob("*.py"):
        source = path.read_text()
        for match in re.finditer(recorders + r"\((.*?)\)", source, re.S):
            gate = re.search(r"gate_id=([A-Za-z_][A-Za-z_0-9]*|\"[a-z_]+\")", match.group(1))
            if gate is None:
                continue
            token = gate.group(1)
            if token == "gate_id":
                continue
            resolved = (
                token.strip('"') if token.startswith('"') else getattr(runtime, token, None) or getattr(wu, token, None)
            )
            if isinstance(resolved, str):
                seamed.add(resolved)

    assert seamed, "found no gate ids; the seam-detection regex needs updating"
    unreachable = seamed - AUTHOR_TIME_GATE_LOG_ONLY_IDS
    assert not unreachable, (
        f"these gates have a suppression seam but the flag cannot disable them: {sorted(unreachable)}. "
        "Add them to AUTHOR_TIME_GATE_LOG_ONLY_IDS."
    )
    eventless = AUTHOR_TIME_GATE_LOG_ONLY_IDS - seamed
    assert not eventless, (
        f"these flag-eligible gates emit no ablation events when suppressed: {sorted(eventless)}. "
        "Suppression without events cannot be graded or monitored — add a recording seam."
    )


_SECURITY_GATE_IDS = frozenset(
    {
        "raw_secret_leak",
        "raw_secret_handling",
        "code_safety_reject",
        "credential_reference_presence",
        "unapproved_credential_reference",
        "credential_scout_reopen",
    }
)


def test_eligible_gate_ids_are_exactly_the_sanctioned_seven() -> None:
    """Security-critical: OutputPolicy security gates must never become flag-suppressible.
    Exact-content assertion is intentional — any change to this set is a security decision."""
    assert AUTHOR_TIME_GATE_LOG_ONLY_IDS == frozenset(
        {
            "definition_contract_unsatisfied",
            "recorded_outcome_grounding_binder_ceiling",
            "synthesized_parameter_binding_ambiguous",
            "output_contract_actuation",
            "metadata_run_preflight_reject",
            "uncovered_output_rescout_steer",
            "recorded_outcome_grounding",
        }
    )
    assert not (AUTHOR_TIME_GATE_LOG_ONLY_IDS & _SECURITY_GATE_IDS)
