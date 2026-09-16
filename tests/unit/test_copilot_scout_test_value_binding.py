"""Tests for model-owned run inputs and the required_input_unbound outcome."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from skyvern.forge.sdk.copilot.build_test_outcome import (
    authored_block_parameter_keys_from_workflow,
    recorded_outcome_from_run_blocks_result,
)
from skyvern.forge.sdk.copilot.run_outcome import RecordedRunOutcome
from skyvern.forge.sdk.copilot.tools.run_execution import (
    _ephemeral_input_values_by_parameter_key,
    _resolve_run_data_and_unbound_keys,
)
from skyvern.forge.sdk.copilot.tools.workflow_update import _input_binding_violations
from skyvern.forge.sdk.workflow.models.parameter import WorkflowParameter, WorkflowParameterType


def _wp(
    key: str,
    *,
    default_value: str | None = None,
    ptype: WorkflowParameterType = WorkflowParameterType.STRING,
) -> WorkflowParameter:
    now = datetime.now(timezone.utc)
    return WorkflowParameter(
        workflow_parameter_id=f"wp_{key}",
        workflow_parameter_type=ptype,
        key=key,
        description=None,
        workflow_id="wf_id",
        default_value=default_value,
        created_at=now,
        modified_at=now,
    )


def test_model_authored_default_supplies_run_value() -> None:
    data, unbound = _resolve_run_data_and_unbound_keys([_wp("specialty", default_value="cardiology")], {})
    assert data == {"specialty": "cardiology"}
    assert unbound == []


def test_explicit_run_parameter_wins_over_model_authored_default() -> None:
    data, unbound = _resolve_run_data_and_unbound_keys(
        [_wp("specialty", default_value="cardiology")],
        {"specialty": "neurology"},
    )
    assert data == {"specialty": "neurology"}
    assert unbound == []


def test_missing_model_owned_value_is_recorded_unbound() -> None:
    data, unbound = _resolve_run_data_and_unbound_keys([_wp("specialty")], {})
    assert data == {"specialty": ""}
    assert unbound == ["specialty"]


def test_at_will_credential_is_omitted_without_placeholder_or_unbound() -> None:
    data, unbound = _resolve_run_data_and_unbound_keys(
        [_wp("maybe_cred", ptype=WorkflowParameterType.CREDENTIAL_ID)], {}
    )
    assert "maybe_cred" not in data
    assert unbound == []


def test_explicit_same_turn_input_binding_supplies_private_run_value() -> None:
    trajectory = [
        {
            "tool_name": "type_text",
            "input_id": "input_opaque_1",
            "input_value": "cardiology",
            "selector": "#specialty",
        }
    ]
    metadata = {
        "search_block": {
            "input_bindings": [{"parameter_key": "specialty", "input_id": "input_opaque_1"}],
        }
    }

    private_values = _ephemeral_input_values_by_parameter_key(metadata, trajectory)
    data, unbound = _resolve_run_data_and_unbound_keys([_wp("specialty")], {}, ephemeral_input_values=private_values)

    assert data == {"specialty": "cardiology"}
    assert unbound == []
    assert "cardiology" not in repr(metadata)


def test_scout_value_is_not_dispatched_without_explicit_model_binding() -> None:
    trajectory = [{"tool_name": "type_text", "input_id": "input_opaque_1", "input_value": "cardiology"}]

    assert _ephemeral_input_values_by_parameter_key({}, trajectory) == {}


def test_input_binding_validator_rejects_unknown_keys_and_identities_without_rewriting() -> None:
    violations = _input_binding_violations(
        block_label="search_block",
        bindings=[
            {"parameter_key": "server_minted_key", "input_id": "missing_input"},
            {
                "parameter_key": "specialty",
                "credential_id": "cred_missing",
                "credential_field": "password",
            },
        ],
        declared_parameter_keys={"specialty"},
        block_parameter_keys={"specialty"},
        scout_trajectory=[{"tool_name": "type_text", "input_id": "input_opaque_1"}],
    )

    assert any("server_minted_key" in violation and "not declared" in violation for violation in violations)
    assert any("cred_missing" in violation and "not present" in violation for violation in violations)


def test_input_binding_validator_rejects_a_carried_input_identity() -> None:
    # A carried identity crosses the turn boundary but the private value it names does not, so
    # the binding resolves to nothing at dispatch. A site that tolerates the empty value would
    # let the run report success on a value nobody demonstrated.
    violations = _input_binding_violations(
        block_label="search_block",
        bindings=[{"parameter_key": "specialty", "input_id": "input_opaque_prior"}],
        declared_parameter_keys={"specialty"},
        block_parameter_keys={"specialty"},
        scout_trajectory=[{"tool_name": "type_text", "input_id": "input_opaque_prior", "carried": True}],
    )

    assert len(violations) == 1
    assert "input_opaque_prior" in violations[0]
    assert "same-turn scout facts" in violations[0]


def test_input_binding_validator_still_accepts_a_carried_credential_identity() -> None:
    # Credentials resolve by credential_id at dispatch, not through the turn-ephemeral value
    # map, so a carried credential identity still binds to a real value.
    violations = _input_binding_violations(
        block_label="login_block",
        bindings=[{"parameter_key": "login_credential", "credential_id": "cred_1", "credential_field": "password"}],
        declared_parameter_keys={"login_credential"},
        block_parameter_keys={"login_credential"},
        scout_trajectory=[
            {
                "tool_name": "fill_credential_field",
                "credential_id": "cred_1",
                "credential_field": "password",
                "carried": True,
            }
        ],
    )

    assert violations == []


def test_input_binding_validator_accepts_exact_ordinary_and_credential_identities() -> None:
    violations = _input_binding_violations(
        block_label="search_block",
        bindings=[
            {"parameter_key": "specialty", "input_id": "input_opaque_1"},
            {
                "parameter_key": "login_credential",
                "credential_id": "cred_1",
                "credential_field": "password",
            },
        ],
        declared_parameter_keys={"specialty", "login_credential"},
        block_parameter_keys={"specialty", "login_credential"},
        scout_trajectory=[
            {"tool_name": "type_text", "input_id": "input_opaque_1"},
            {
                "tool_name": "fill_credential_field",
                "credential_id": "cred_1",
                "credential_field": "password",
            },
        ],
    )

    assert violations == []


_WORKFLOW_YAML = """
workflow_definition:
  parameters:
    - key: search_by_specialty
      parameter_type: workflow
  blocks:
    - block_type: code
      label: search_block
      code: "await page.fill('#q', str(search_by_specialty))"
      parameter_keys:
        - search_by_specialty
""".strip()


def _failed_run_result(*, failure_reason: str, label: str = "search_block", ok: bool = False) -> dict[str, Any]:
    return {
        "ok": ok,
        "data": {
            "workflow_run_id": "wr_test",
            "overall_status": "failed",
            "blocks": [{"label": label, "status": "failed", "failure_reason": failure_reason}],
        },
    }


_LOCATOR_WAIT_REASON = "Timeout 30000ms exceeded waiting for locator('#coastalCard') to be visible"


def test_authored_block_parameter_keys_from_workflow() -> None:
    mapping = authored_block_parameter_keys_from_workflow(_WORKFLOW_YAML)
    assert mapping == {"search_block": ["search_by_specialty"]}


def test_required_input_unbound_fires_when_failed_block_references_key() -> None:
    outcome = recorded_outcome_from_run_blocks_result(
        _failed_run_result(failure_reason=_LOCATOR_WAIT_REASON),
        unbound_required_parameter_keys=["search_by_specialty"],
        block_parameter_keys={"search_block": ["search_by_specialty"]},
    )
    assert outcome is not None
    assert outcome.reason_code == "required_input_unbound"


def test_required_input_unbound_authoritative_on_non_locator_wait_failure() -> None:
    outcome = recorded_outcome_from_run_blocks_result(
        _failed_run_result(failure_reason="some unrelated runtime error"),
        unbound_required_parameter_keys=["search_by_specialty"],
        block_parameter_keys={"search_block": ["search_by_specialty"]},
    )
    assert outcome is not None
    assert outcome.reason_code == "required_input_unbound"
    assert outcome.is_authoritative
    assert outcome.structural_key is not None


def test_required_input_unbound_identity_is_key_order_insensitive() -> None:
    block_parameter_keys = {"search_block": ["search_by_specialty", "search_by_location"]}
    ascending = recorded_outcome_from_run_blocks_result(
        _failed_run_result(failure_reason="some unrelated runtime error"),
        unbound_required_parameter_keys=["search_by_specialty", "search_by_location"],
        block_parameter_keys=block_parameter_keys,
    )
    descending = recorded_outcome_from_run_blocks_result(
        _failed_run_result(failure_reason="some unrelated runtime error"),
        unbound_required_parameter_keys=["search_by_location", "search_by_specialty"],
        block_parameter_keys=block_parameter_keys,
    )
    assert ascending is not None and descending is not None
    assert ascending.structural_key == descending.structural_key


def test_terminal_challenge_blocker_wins_over_required_input_unbound() -> None:
    outcome = recorded_outcome_from_run_blocks_result(
        _failed_run_result(failure_reason=_LOCATOR_WAIT_REASON),
        recorded_run_outcome=RecordedRunOutcome(verdict="not_demonstrated", reason_code="terminal_challenge_blocker"),
        unbound_required_parameter_keys=["search_by_specialty"],
        block_parameter_keys={"search_block": ["search_by_specialty"]},
    )
    assert outcome is not None
    assert outcome.reason_code == "terminal_challenge_blocker"


def test_legacy_demonstrated_run_cannot_override_required_input_unbound() -> None:
    outcome = recorded_outcome_from_run_blocks_result(
        _failed_run_result(failure_reason=_LOCATOR_WAIT_REASON),
        recorded_run_outcome=RecordedRunOutcome(verdict="demonstrated"),
        unbound_required_parameter_keys=["search_by_specialty"],
        block_parameter_keys={"search_block": ["search_by_specialty"]},
    )
    assert outcome is not None
    assert outcome.reason_code == "required_input_unbound"


def test_required_input_unbound_wins_over_an_unevaluated_run() -> None:
    outcome = recorded_outcome_from_run_blocks_result(
        _failed_run_result(failure_reason=_LOCATOR_WAIT_REASON),
        recorded_run_outcome=RecordedRunOutcome(verdict="not_evaluated"),
        unbound_required_parameter_keys=["search_by_specialty"],
        block_parameter_keys={"search_block": ["search_by_specialty"]},
    )
    assert outcome is not None
    assert outcome.reason_code == "required_input_unbound"


def test_required_input_unbound_not_fired_when_block_does_not_reference_key() -> None:
    outcome = recorded_outcome_from_run_blocks_result(
        _failed_run_result(failure_reason=_LOCATOR_WAIT_REASON),
        unbound_required_parameter_keys=["some_other_key"],
        block_parameter_keys={"search_block": ["search_by_specialty"]},
    )
    assert outcome is not None
    assert outcome.reason_code == "runtime_block_failure"


def test_required_input_unbound_not_fired_when_payload_missing() -> None:
    outcome = recorded_outcome_from_run_blocks_result(
        _failed_run_result(failure_reason=_LOCATOR_WAIT_REASON),
        unbound_required_parameter_keys=["search_by_specialty"],
        block_parameter_keys={},
    )
    assert outcome is not None
    assert outcome.reason_code == "runtime_block_failure"


def test_required_input_unbound_not_fired_on_success() -> None:
    result = _failed_run_result(failure_reason=_LOCATOR_WAIT_REASON, ok=True)
    result["data"]["overall_status"] = "completed"
    outcome = recorded_outcome_from_run_blocks_result(
        result,
        unbound_required_parameter_keys=["search_by_specialty"],
        block_parameter_keys={"search_block": ["search_by_specialty"]},
    )
    if outcome is not None:
        assert outcome.reason_code != "required_input_unbound"
