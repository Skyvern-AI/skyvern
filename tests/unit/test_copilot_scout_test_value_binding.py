"""Tests for model-owned run inputs and the required_input_unbound outcome."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock

import pytest

from skyvern.constants import SCRUBBED_VALUE
from skyvern.forge import app as forge_app
from skyvern.forge.sdk.copilot.build_test_outcome import (
    authored_block_parameter_keys_from_workflow,
    recorded_outcome_from_run_blocks_result,
)
from skyvern.forge.sdk.copilot.output_utils import sanitize_tool_result_for_llm
from skyvern.forge.sdk.copilot.run_outcome import RecordedRunOutcome
from skyvern.forge.sdk.copilot.tools.run_execution import (
    _ephemeral_input_values_by_parameter_key,
    _resolve_run_data_and_unbound_keys,
    _run_blocks_and_collect_debug,
    finalize_build_test_result,
    reusable_origin_input_keys,
)
from skyvern.forge.sdk.copilot.tools.workflow_update import _input_binding_violations
from skyvern.forge.sdk.copilot.workflow_yaml import _process_workflow_yaml
from skyvern.forge.sdk.workflow.models.parameter import WorkflowParameter, WorkflowParameterType
from skyvern.services import workflow_service as workflow_service_module
from tests.unit.copilot_test_helpers import (
    install_run_blocks_harness,
    make_copilot_ctx,
    origin_run_input,
    terminal_extraction_block,
)


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
    data, unbound, reused = _resolve_run_data_and_unbound_keys([_wp("specialty", default_value="cardiology")], {})
    assert data == {"specialty": "cardiology"}
    assert unbound == []
    assert reused == []


def test_explicit_run_parameter_wins_over_model_authored_default() -> None:
    data, unbound, reused = _resolve_run_data_and_unbound_keys(
        [_wp("specialty", default_value="cardiology")],
        {"specialty": "neurology"},
    )
    assert data == {"specialty": "neurology"}
    assert unbound == []
    assert reused == []


def test_missing_model_owned_value_is_recorded_unbound() -> None:
    data, unbound, reused = _resolve_run_data_and_unbound_keys([_wp("specialty")], {})
    assert data == {"specialty": ""}
    assert unbound == ["specialty"]
    assert reused == []


def test_at_will_credential_is_omitted_without_placeholder_or_unbound() -> None:
    data, unbound, reused = _resolve_run_data_and_unbound_keys(
        [_wp("maybe_cred", ptype=WorkflowParameterType.CREDENTIAL_ID)], {}
    )
    assert "maybe_cred" not in data
    assert unbound == []
    assert reused == []


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
    data, unbound, reused = _resolve_run_data_and_unbound_keys(
        [_wp("specialty")], {}, ephemeral_input_values=private_values
    )

    assert data == {"specialty": "cardiology"}
    assert unbound == []
    assert reused == []
    assert "cardiology" not in repr(metadata)


_ORIGIN_FILE = "s3://origin-sentinel-bucket/uploads/resume-sentinel.pdf"


def test_origin_run_value_binds_a_required_input_with_no_default() -> None:
    data, unbound, reused = _resolve_run_data_and_unbound_keys(
        [_wp("resume", ptype=WorkflowParameterType.FILE_URL)],
        {},
        origin_parameters=[origin_run_input("resume", _ORIGIN_FILE)],
    )
    assert data == {"resume": _ORIGIN_FILE}
    assert unbound == []
    assert reused == ["resume"]


@pytest.mark.parametrize(
    ("user_params", "ephemeral", "default_value", "expected", "expected_reused"),
    [
        ({"resume": "model_file"}, None, None, "model_file", []),
        ({}, {"resume": "scout_file"}, None, "scout_file", []),
        ({}, None, "default_file", _ORIGIN_FILE, ["resume"]),
    ],
    ids=["model_beats_origin", "ephemeral_beats_origin", "origin_beats_default"],
)
def test_origin_run_value_precedence(
    user_params: dict[str, str],
    ephemeral: dict[str, str] | None,
    default_value: str | None,
    expected: str,
    expected_reused: list[str],
) -> None:
    data, unbound, reused = _resolve_run_data_and_unbound_keys(
        [_wp("resume", default_value=default_value, ptype=WorkflowParameterType.FILE_URL)],
        user_params,
        ephemeral_input_values=ephemeral,
        origin_parameters=[origin_run_input("resume", _ORIGIN_FILE)],
    )
    assert data == {"resume": expected}
    assert unbound == []
    assert reused == expected_reused


def test_origin_run_value_of_another_type_is_not_reused() -> None:
    data, unbound, reused = _resolve_run_data_and_unbound_keys(
        [_wp("resume", ptype=WorkflowParameterType.FILE_URL)],
        {},
        origin_parameters=[origin_run_input("resume", _ORIGIN_FILE, ptype=WorkflowParameterType.STRING)],
    )
    assert data == {"resume": ""}
    assert unbound == ["resume"]
    assert reused == []


@pytest.mark.parametrize(
    ("origin_is_copilot_run", "expected_unbound", "expected_reused"),
    [(True, ["attempts"], []), (False, [], ["attempts"])],
    ids=["copilot_test_run", "user_run"],
)
def test_origin_run_placeholder_is_absent_only_when_a_copilot_test_run_stored_it(
    origin_is_copilot_run: bool, expected_unbound: list[str], expected_reused: list[str]
) -> None:
    data, unbound, reused = _resolve_run_data_and_unbound_keys(
        [_wp("attempts", ptype=WorkflowParameterType.INTEGER)],
        {},
        origin_parameters=[origin_run_input("attempts", 0, ptype=WorkflowParameterType.INTEGER)],
        origin_is_copilot_run=origin_is_copilot_run,
    )
    assert data == {"attempts": 0}
    assert unbound == expected_unbound
    assert reused == expected_reused


def test_scrubbed_origin_file_is_not_reused() -> None:
    data, unbound, reused = _resolve_run_data_and_unbound_keys(
        [_wp("resume", ptype=WorkflowParameterType.FILE_URL)],
        {},
        origin_parameters=[origin_run_input("resume", SCRUBBED_VALUE)],
    )
    assert data == {"resume": ""}
    assert unbound == ["resume"]
    assert reused == []


def test_origin_run_credential_is_not_reused() -> None:
    data, _, reused = _resolve_run_data_and_unbound_keys(
        [_wp("login", ptype=WorkflowParameterType.CREDENTIAL_ID)],
        {},
        origin_parameters=[origin_run_input("login", "cred_origin", ptype=WorkflowParameterType.CREDENTIAL_ID)],
    )
    assert "login" not in data
    assert reused == []


def test_origin_run_value_equal_to_its_own_default_is_not_reused() -> None:
    data, unbound, reused = _resolve_run_data_and_unbound_keys(
        [_wp("resume", default_value="current_default_file", ptype=WorkflowParameterType.FILE_URL)],
        {},
        origin_parameters=[origin_run_input("resume", "origin_default_file", default_value="origin_default_file")],
    )
    assert data == {"resume": "current_default_file"}
    assert unbound == []
    assert reused == []


@pytest.mark.parametrize(
    ("declared_type", "origin_type", "origin_value", "origin_is_copilot_run", "expected"),
    [
        (WorkflowParameterType.FILE_URL, WorkflowParameterType.FILE_URL, _ORIGIN_FILE, False, ["resume"]),
        (WorkflowParameterType.STRING, WorkflowParameterType.FILE_URL, _ORIGIN_FILE, False, []),
        (WorkflowParameterType.FILE_URL, WorkflowParameterType.FILE_URL, SCRUBBED_VALUE, False, []),
        (WorkflowParameterType.INTEGER, WorkflowParameterType.INTEGER, 0, False, ["resume"]),
        (WorkflowParameterType.INTEGER, WorkflowParameterType.INTEGER, 0, True, []),
    ],
    ids=["same_type", "other_type", "scrubbed", "user_run_placeholder", "copilot_run_placeholder"],
)
@pytest.mark.asyncio
async def test_reusable_keys_follow_the_resolver_rule_for_the_current_workflow(
    monkeypatch: pytest.MonkeyPatch,
    declared_type: WorkflowParameterType,
    origin_type: WorkflowParameterType,
    origin_value: str | int,
    origin_is_copilot_run: bool,
    expected: list[str],
) -> None:
    monkeypatch.setattr(forge_app.WORKFLOW_SERVICE, "get_workflow_by_permanent_id", AsyncMock(return_value=None))
    ctx = make_copilot_ctx()
    ctx.staged_workflow = await _process_workflow_yaml(
        settings_fallback_yaml="enable_self_healing: false",
        workflow_id="w_source",
        workflow_permanent_id="wfp-1",
        organization_id="org-1",
        workflow_yaml=_FILE_INPUT_WORKFLOW_YAML.replace("file_url", declared_type.value),
    )
    ctx.repair_origin_input_values = (
        origin_run_input("resume", origin_value, origin_type),
        origin_run_input("dropped", _ORIGIN_FILE),
    )
    ctx.repair_origin_is_copilot_run = origin_is_copilot_run

    assert await reusable_origin_input_keys(ctx) == expected


_FILE_INPUT_WORKFLOW_YAML = """
title: upload a resume
workflow_definition:
  parameters:
    - parameter_type: workflow
      workflow_parameter_type: file_url
      key: resume
  blocks:
    - block_type: extraction
      label: extract_heading
      url: https://example.com
      data_extraction_goal: Extract the page heading.
      parameter_keys:
        - resume
"""


@pytest.mark.asyncio
async def test_reused_origin_value_is_dispatched_but_only_its_key_reaches_the_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = await install_run_blocks_harness(
        monkeypatch,
        workflow_yaml=_FILE_INPUT_WORKFLOW_YAML,
        polled_status="failed",
        terminal_blocks=[terminal_extraction_block("failed")],
    )
    ctx = make_copilot_ctx(browser_session_id="pbs_chat")
    ctx.staged_workflow = harness["workflow"]
    ctx.frontier_resume_session_id = "pbs_run"
    ctx.repair_origin_input_values = (origin_run_input("resume", _ORIGIN_FILE),)

    result = await _run_blocks_and_collect_debug({"block_labels": ["extract_heading"], "parameters": {}}, ctx)
    finalize_build_test_result(ctx, source_tool="run_blocks_and_collect_debug", result=result)
    model_visible = json.dumps(sanitize_tool_result_for_llm("run_blocks_and_collect_debug", result))

    dispatched = workflow_service_module.prepare_workflow.call_args.kwargs["workflow_request"].data
    assert dispatched["resume"] == _ORIGIN_FILE
    assert ctx.unbound_required_parameter_keys == []
    assert '"reused_origin_input_keys": ["resume"]' in model_visible
    assert _ORIGIN_FILE not in model_visible


@pytest.mark.asyncio
async def test_a_copilot_test_runs_stored_placeholder_leaves_the_test_run_input_unbound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = await install_run_blocks_harness(
        monkeypatch,
        workflow_yaml=_FILE_INPUT_WORKFLOW_YAML.replace("file_url", "integer"),
        polled_status="failed",
        terminal_blocks=[terminal_extraction_block("failed")],
    )
    ctx = make_copilot_ctx(browser_session_id="pbs_chat")
    ctx.staged_workflow = harness["workflow"]
    ctx.frontier_resume_session_id = "pbs_run"
    ctx.repair_origin_input_values = (origin_run_input("resume", 0, WorkflowParameterType.INTEGER),)
    ctx.repair_origin_is_copilot_run = True

    result = await _run_blocks_and_collect_debug({"block_labels": ["extract_heading"], "parameters": {}}, ctx)

    assert ctx.unbound_required_parameter_keys == ["resume"]
    assert "reused_origin_input_keys" not in result["data"]


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
