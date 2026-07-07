"""Tests for the scout->test-run value binding lane and the required_input_unbound outcome.

OSS-synced: only RFC-2606 / localhost-mock placeholder data ('Jordan Avery', example.com).
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from skyvern.forge.sdk.copilot.build_test_outcome import (
    _binding_frontier_facet,
    authored_block_parameter_keys_from_workflow,
    recorded_outcome_from_run_blocks_result,
)
from skyvern.forge.sdk.copilot.code_block_synthesis import (
    _typed_value_identity,
    synthesize_code_block,
)
from skyvern.forge.sdk.copilot.reached_download_target import ReachedDownloadTarget
from skyvern.forge.sdk.copilot.run_outcome import RecordedRunOutcome
from skyvern.forge.sdk.copilot.tools.run_execution import (
    _resolve_run_data_and_unbound_keys,
    _scout_ephemeral_values,
)
from skyvern.forge.sdk.copilot.tools.scouting import _record_scouted_interaction
from skyvern.forge.sdk.workflow.models.parameter import WorkflowParameter, WorkflowParameterType

SCOUT_VALUE = "Jordan Avery"


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


def _type_text(
    accessible_name: str,
    *,
    raw: str,
    typed_value: str = "",
    index: int = 0,
    selector: str | None = None,
) -> dict[str, Any]:
    interaction: dict[str, Any] = {
        "tool_name": "type_text",
        "source_url": "https://example.com/search",
        "role": "textbox",
        "accessible_name": accessible_name,
        "typed_length": len(raw),
        "trajectory_index": index,
    }
    resolved_selector = f"#{accessible_name.lower().replace(' ', '_')}" if selector is None else selector
    if resolved_selector:
        interaction["selector"] = resolved_selector
    if typed_value:
        interaction["typed_value"] = typed_value
    if raw:
        interaction["raw_typed_value"] = raw
    return interaction


class _Ctx:
    def __init__(
        self,
        trajectory: list[dict[str, Any]],
        *,
        reached_download_target: ReachedDownloadTarget | None = None,
    ) -> None:
        self.scout_trajectory = trajectory
        self.reached_download_target = reached_download_target


def _minted_key(trajectory: list[dict[str, Any]]) -> str:
    synthesized = synthesize_code_block(trajectory)
    assert synthesized is not None
    bindings = synthesized.diagnostics.typed_param_bindings
    assert bindings
    return bindings[0][1]


def test_scout_binds_when_default_absent() -> None:
    trajectory = [_type_text("Provider name", raw=SCOUT_VALUE)]
    key = _minted_key(trajectory)
    ephemeral = _scout_ephemeral_values(_Ctx(trajectory), {key})
    assert ephemeral == {key: SCOUT_VALUE}
    data, unbound = _resolve_run_data_and_unbound_keys([_wp(key)], {}, ephemeral)
    assert data[key] == SCOUT_VALUE
    assert unbound == []


def test_scout_binds_when_default_empty_string() -> None:
    trajectory = [_type_text("Provider name", raw=SCOUT_VALUE)]
    key = _minted_key(trajectory)
    ephemeral = _scout_ephemeral_values(_Ctx(trajectory), {key})
    data, unbound = _resolve_run_data_and_unbound_keys([_wp(key, default_value="")], {}, ephemeral)
    assert data[key] == SCOUT_VALUE
    assert unbound == []


def test_non_empty_default_wins_over_scout() -> None:
    trajectory = [_type_text("Provider name", raw=SCOUT_VALUE)]
    key = _minted_key(trajectory)
    ephemeral = _scout_ephemeral_values(_Ctx(trajectory), {key})
    data, unbound = _resolve_run_data_and_unbound_keys([_wp(key, default_value="cardiology")], {}, ephemeral)
    assert data[key] == "cardiology"
    assert unbound == []


def test_user_param_wins_over_scout() -> None:
    trajectory = [_type_text("Provider name", raw=SCOUT_VALUE)]
    key = _minted_key(trajectory)
    ephemeral = _scout_ephemeral_values(_Ctx(trajectory), {key})
    data, unbound = _resolve_run_data_and_unbound_keys([_wp(key)], {key: "user-supplied"}, ephemeral)
    assert data[key] == "user-supplied"
    assert unbound == []


def test_same_name_selectorless_sibling_drift_falls_safe() -> None:
    trajectory = [
        _type_text("Provider name", raw="Casey Nguyen", selector="", index=0),
        _type_text("Provider name", raw=SCOUT_VALUE, selector="#provider", index=1),
    ]
    strict = synthesize_code_block(trajectory, strict_selectors=True)
    lenient = synthesize_code_block(trajectory, strict_selectors=False)
    assert strict is not None and lenient is not None
    strict_key = dict(strict.diagnostics.typed_param_bindings)[1]
    assert dict(lenient.diagnostics.typed_param_bindings)[1] != strict_key
    ephemeral = _scout_ephemeral_values(_Ctx(trajectory), {strict_key})
    assert ephemeral == {}


def test_distinct_named_field_binds_despite_dropped_selectorless_sibling() -> None:
    trajectory = [
        _type_text("First name", raw="Casey Nguyen", selector="", index=0),
        _type_text("Last name", raw=SCOUT_VALUE, selector="#last", index=1),
    ]
    ephemeral = _scout_ephemeral_values(_Ctx(trajectory), {"last_name"})
    assert ephemeral == {"last_name": SCOUT_VALUE}


def test_no_scout_value_falls_to_placeholder_and_records_unbound() -> None:
    data, unbound = _resolve_run_data_and_unbound_keys([_wp("search")], {}, {})
    assert data["search"] == ""
    assert unbound == ["search"]


def test_empty_string_default_no_scout_records_unbound() -> None:
    data, unbound = _resolve_run_data_and_unbound_keys([_wp("search", default_value="")], {}, {})
    assert unbound == ["search"]


def test_secret_like_scout_value_not_bound() -> None:
    trajectory = [_type_text("Login", raw="user@example.com:hunter2Pass")]
    key = _minted_key(trajectory)
    ephemeral = _scout_ephemeral_values(_Ctx(trajectory), {key})
    assert ephemeral == {}


def test_secret_field_context_scout_value_not_bound() -> None:
    trajectory = [_type_text("Password", raw="Redwood", selector="#password")]
    key = _minted_key(trajectory)
    ephemeral = _scout_ephemeral_values(_Ctx(trajectory), {key})
    assert ephemeral == {}


def test_ambiguous_duplicate_value_falls_safe_to_unbound() -> None:
    trajectory = [
        _type_text("First field", raw=SCOUT_VALUE, index=0),
        _type_text("Second field", raw=SCOUT_VALUE, index=1),
    ]
    synthesized = synthesize_code_block(trajectory)
    assert synthesized is not None
    keys = {key for _, key in synthesized.diagnostics.typed_param_bindings}
    ephemeral = _scout_ephemeral_values(_Ctx(trajectory), keys)
    assert ephemeral == {}


def test_key_not_in_workflow_falls_safe() -> None:
    trajectory = [_type_text("Provider name", raw=SCOUT_VALUE)]
    ephemeral = _scout_ephemeral_values(_Ctx(trajectory), {"unrelated_key"})
    assert ephemeral == {}


def test_empty_trajectory_no_crash_no_bind() -> None:
    assert _scout_ephemeral_values(_Ctx([]), {"search"}) == {}


def test_record_scouted_interaction_carries_raw_value_into_trajectory() -> None:
    ctx = SimpleNamespace(
        scouted_interactions=[],
        scout_trajectory=[],
        last_evaluate_actionable_signature=None,
        last_evaluate_actionable_url=None,
        latest_evaluate_result_composition_steer=None,
    )
    _record_scouted_interaction(
        ctx,
        tool_name="type_text",
        selector="#provider",
        source_url="https://example.com/search",
        typed_length=len(SCOUT_VALUE),
        typed_value="",
        raw_typed_value=SCOUT_VALUE,
        role="textbox",
        accessible_name="Provider name",
    )
    assert ctx.scout_trajectory[0]["raw_typed_value"] == SCOUT_VALUE
    assert "typed_value" not in ctx.scout_trajectory[0]


def test_raw_value_excluded_from_typed_value_identity() -> None:
    interaction = _type_text("Provider name", raw=SCOUT_VALUE)
    assert _typed_value_identity(interaction) is None


def test_raw_value_not_promoted_to_default_value() -> None:
    trajectory = [_type_text("Provider name", raw=SCOUT_VALUE)]
    synthesized = synthesize_code_block(trajectory)
    assert synthesized is not None
    assert all("default_value" not in parameter for parameter in synthesized.parameters)


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
    assert _binding_frontier_facet(outcome) == "amend_in_place"


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


def test_demonstrated_run_wins_over_required_input_unbound() -> None:
    outcome = recorded_outcome_from_run_blocks_result(
        _failed_run_result(failure_reason=_LOCATOR_WAIT_REASON),
        recorded_run_outcome=RecordedRunOutcome(verdict="demonstrated"),
        unbound_required_parameter_keys=["search_by_specialty"],
        block_parameter_keys={"search_block": ["search_by_specialty"]},
    )
    assert outcome is not None
    assert outcome.reason_code == "verified_success"


def test_not_evaluated_run_wins_over_required_input_unbound() -> None:
    outcome = recorded_outcome_from_run_blocks_result(
        _failed_run_result(failure_reason=_LOCATOR_WAIT_REASON),
        recorded_run_outcome=RecordedRunOutcome(verdict="not_evaluated"),
        unbound_required_parameter_keys=["search_by_specialty"],
        block_parameter_keys={"search_block": ["search_by_specialty"]},
    )
    assert outcome is not None
    assert outcome.reason_code != "required_input_unbound"


def test_required_input_unbound_not_fired_when_block_does_not_reference_key() -> None:
    outcome = recorded_outcome_from_run_blocks_result(
        _failed_run_result(failure_reason=_LOCATOR_WAIT_REASON),
        unbound_required_parameter_keys=["some_other_key"],
        block_parameter_keys={"search_block": ["search_by_specialty"]},
    )
    assert outcome is not None
    assert outcome.reason_code == "runtime_block_failure"
    assert _binding_frontier_facet(outcome) == "selector_frontier"


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
