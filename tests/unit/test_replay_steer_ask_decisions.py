from __future__ import annotations

import json
from pathlib import Path

from dev_scripts.replay_steer_ask_decisions import replay_ask


def _write_packet(path: Path, **overrides: object) -> Path:
    payload: dict[str, object] = {
        "outcome": "auto_answered",
        "ask_subject": "output_schema",
        "ask_refs": ["azure_error_count"],
        "admission_base": False,
        "has_genuine_workflow_attempt": False,
        "request_policy_user_response_policy": "proceed",
        "request_policy_clarification_reason": "none",
        "request_policy_has_present_completion_contract": False,
        "turn_intent_mode": "build",
        "turn_intent_expected_output": "run_result",
        "turn_intent_may_update_workflow": True,
        "turn_intent_may_run_blocks": True,
        "turn_intent_requires_user_input": False,
        "criteria": [],
        "satisfied_criterion_ids": [],
    }
    payload.update(overrides)
    path.write_text(json.dumps(payload))
    return path


def test_replay_ask_recomputes_run_result_output_schema_authority(tmp_path: Path) -> None:
    recorded, replayed, detail = replay_ask(_write_packet(tmp_path / "ask-auto_answered.json"))

    assert recorded == "auto_answered"
    assert replayed == "auto_answered"
    assert "authority=turn_intent_run_result" in detail


def test_replay_ask_detects_workflow_update_output_schema_flip(tmp_path: Path) -> None:
    packet = _write_packet(
        tmp_path / "ask-reached_user.json",
        outcome="reached_user",
        turn_intent_expected_output="workflow_update",
    )

    recorded, replayed, detail = replay_ask(packet)

    assert recorded == "reached_user"
    assert replayed == "auto_answered"
    assert "authority=turn_intent_update_and_run" in detail


def test_replay_ask_marks_old_run_result_packet_capture_limited(tmp_path: Path) -> None:
    packet = _write_packet(tmp_path / "ask-auto_answered.json")
    payload = json.loads(packet.read_text())
    del payload["turn_intent_may_run_blocks"]
    packet.write_text(json.dumps(payload))

    _recorded, replayed, _detail = replay_ask(packet)

    assert replayed == "CAPTURE-LIMITED"


def test_replay_ask_rejects_string_boolean_authority(tmp_path: Path) -> None:
    packet = _write_packet(tmp_path / "ask-invalid-bool.json", turn_intent_may_run_blocks="false")

    _recorded, replayed, detail = replay_ask(packet)

    assert replayed == "CAPTURE-LIMITED"
    assert "authority is invalid" in detail


def test_replay_ask_rejects_unknown_clarification_reason(tmp_path: Path) -> None:
    packet = _write_packet(
        tmp_path / "ask-invalid-reason.json",
        request_policy_clarification_reason="not-a-real-reason",
    )

    _recorded, replayed, detail = replay_ask(packet)

    assert replayed == "CAPTURE-LIMITED"
    assert "authority is invalid" in detail
