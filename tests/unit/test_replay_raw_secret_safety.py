from __future__ import annotations

import json
from pathlib import Path

import pytest

from dev_scripts.replay_raw_secret_safety import ReplayCase, ReplaySample, load_replay_cases, sample_matches_expected


def test_load_replay_cases_accepts_literal_message() -> None:
    assert load_replay_cases(["try again"]) == [ReplayCase(name="message-1", message="try again")]


def test_load_replay_cases_accepts_eval_dataset_and_dedicated_dump(tmp_path: Path) -> None:
    dataset = tmp_path / "cases.yaml"
    dataset.write_text(
        """\
name: clean_regressions
cases:
  - name: saved_credential_followup
    message: "that's not a secret"
    state: clean
    handling: none
    citations: []
"""
    )
    dump = tmp_path / "raw-secret-safety-captured.json"
    dump.write_text(
        json.dumps(
            {
                "name": "captured_turn",
                "message": "try again",
                "organization_id": "org-captured",
                "workflow_permanent_id": "wpid-captured",
            }
        )
    )

    assert load_replay_cases([str(dataset), str(dump)]) == [
        ReplayCase(
            name="saved_credential_followup",
            message="that's not a secret",
            expected_state="clean",
            expected_handling="none",
            expected_citations=(),
            source=str(dataset),
        ),
        ReplayCase(
            name="captured_turn",
            message="try again",
            organization_id="org-captured",
            workflow_permanent_id="wpid-captured",
            source=str(dump),
        ),
    ]


def test_load_replay_cases_reads_only_dedicated_dumps_from_directory(tmp_path: Path) -> None:
    (tmp_path / "unrelated.json").write_text('{"message":"ignore me"}')
    (tmp_path / "raw-secret-safety-002.json").write_text('{"name":"second","message":"two"}')
    (tmp_path / "raw-secret-safety-001.json").write_text('{"name":"first","message":"one"}')

    assert [case.name for case in load_replay_cases([str(tmp_path)])] == ["first", "second"]


def test_load_replay_cases_rejects_unrecognized_payload(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text('{"prompt":"ambiguous generic artifact"}')

    with pytest.raises(ValueError, match="message or cases"):
        load_replay_cases([str(bad)])


def test_sample_matches_all_declared_expectations() -> None:
    case = ReplayCase(
        name="clean",
        message="try again",
        expected_state="clean",
        expected_handling="none",
        expected_citations=(),
    )
    clean = ReplaySample(
        model_state="clean",
        model_citations=(),
        policy_status="clean",
        handling="none",
        canonical_user_message="try again",
        failure_kind="none",
        citation_count=0,
        exonerated_citation_count=0,
        allow_update_workflow=True,
        allow_run_blocks=True,
    )
    false_positive = ReplaySample(
        model_state="detected",
        model_citations=("try again",),
        policy_status="detected",
        handling="redacted_draft",
        canonical_user_message="[REDACTED_SECRET]",
        failure_kind="none",
        citation_count=1,
        exonerated_citation_count=0,
        allow_update_workflow=True,
        allow_run_blocks=False,
    )

    assert sample_matches_expected(case, clean) is True
    assert sample_matches_expected(case, false_positive) is False
