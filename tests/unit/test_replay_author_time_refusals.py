from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from dev_scripts.replay_author_time_refusals import (
    CapturedSubmission,
    _captured_submissions,
    _result_satisfies_replay_contract,
    replay_submission,
    retained_hard_block_controls,
)
from skyvern.forge.sdk.copilot.author_time_block import (
    BANNED_BLOCKS_BLOCK_ID,
    CODE_SAFETY_BLOCK_ID,
    CREDENTIAL_SCOUT_BLOCK_ID,
)


def _yaml(body: str) -> str:
    return textwrap.dedent(body).strip() + "\n"


_IMPERFECT_ERROR_CODE_YAML = _yaml(
    """
    title: Undeclared error replay
    mask_secrets: false
    enable_self_healing: false
    pin_saved_session_ip: false
    workflow_definition:
      version: 2
      parameters: []
      blocks:
      - block_type: code
        label: raise_undeclared_error
        error_code_mapping:
          SKY_13894_UNUSED: SKY_13894_UNUSED
        code: |
          raise ErrorCode("SKY_13894_UNDECLARED", "fixture witness")
    """
)


def _submission(tool_name: str, workflow_yaml: str, **arguments: object) -> CapturedSubmission:
    return CapturedSubmission(
        call_id="call_replay",
        tool_name=tool_name,
        arguments={"workflow_yaml": workflow_yaml, **arguments},
        source_path=Path("capture.json"),
    )


def test_cumulative_model_input_dumps_are_deduplicated_by_call_id(tmp_path: Path) -> None:
    first = {
        "type": "function_call",
        "call_id": "call_1",
        "name": "update_workflow",
        "arguments": json.dumps({"workflow_yaml": _IMPERFECT_ERROR_CODE_YAML}),
    }
    second = {
        "type": "function_call",
        "call_id": "call_2",
        "name": "update_and_run_blocks",
        "arguments": json.dumps(
            {"workflow_yaml": _IMPERFECT_ERROR_CODE_YAML, "block_labels": ["raise_undeclared_error"]}
        ),
    }
    (tmp_path / "call-0001.json").write_text(json.dumps({"input": [first]}))
    (tmp_path / "call-0002.json").write_text(json.dumps({"input": [first, second]}))

    submissions = _captured_submissions(tmp_path)

    assert [(item.call_id, item.tool_name) for item in submissions] == [
        ("call_1", "update_workflow"),
        ("call_2", "update_and_run_blocks"),
    ]


@pytest.mark.asyncio
async def test_real_update_handler_persists_imperfect_error_code_payload_once() -> None:
    result = await replay_submission(_submission("update_workflow", _IMPERFECT_ERROR_CODE_YAML))

    assert result["ok"] is True
    assert result["staged_writes"] == 1
    assert result["run_dispatches"] == 0
    assert result["block_id"] is None
    assert result["submitted_yaml_sha256"] == result["effective_yaml_sha256"]
    assert result["effective_yaml_sha256"] == result["staged_yaml_sha256"]


@pytest.mark.asyncio
async def test_real_combined_handler_persists_then_dispatches_once() -> None:
    result = await replay_submission(
        _submission(
            "update_and_run_blocks",
            _IMPERFECT_ERROR_CODE_YAML,
            block_labels=["raise_undeclared_error"],
        )
    )

    assert result["ok"] is False
    assert result["tool_result_error"] == "recording fake runtime failure"
    assert result["staged_writes"] == 1
    assert result["run_dispatches"] == 1
    assert result["run_block_labels"] == ["raise_undeclared_error"]


@pytest.mark.asyncio
async def test_credential_misbinding_does_not_block_or_author_a_finding() -> None:
    misbound = _IMPERFECT_ERROR_CODE_YAML.replace(
        'raise ErrorCode("SKY_13894_UNDECLARED", "fixture witness")',
        'print("cred_fixture_123")',
    ).replace("raise_undeclared_error", "misbound_credential")

    result = await replay_submission(
        _submission("update_workflow", misbound),
        approve_misbound_credentials=True,
    )

    assert result["ok"] is True
    assert result["staged_writes"] == 1
    assert result["finding_reason_codes"] == []


@pytest.mark.asyncio
async def test_only_retained_hard_blocks_prevent_staging() -> None:
    results = [await replay_submission(control) for control in retained_hard_block_controls()]

    assert [result["block_id"] for result in results] == [
        CODE_SAFETY_BLOCK_ID,
        CREDENTIAL_SCOUT_BLOCK_ID,
        BANNED_BLOCKS_BLOCK_ID,
    ]
    assert all(result["ok"] is False for result in results)
    assert all(result["staged_writes"] == 0 for result in results)
    assert all(result["run_dispatches"] == 0 for result in results)


@pytest.mark.asyncio
async def test_malformed_yaml_is_an_honest_tool_error_not_a_refusal() -> None:
    result = await replay_submission(_submission("update_workflow", "workflow_definition: ["))

    assert result["ok"] is False
    assert result["block_id"] is None
    assert result["staged_writes"] == 0
    assert result["run_dispatches"] == 0
    assert result["tool_result_error"].startswith("Workflow validation failed:")


def test_report_contract_requires_combined_tool_dispatch() -> None:
    result = {
        "call_id": "call_combined",
        "tool_name": "update_and_run_blocks",
        "staged_writes": 1,
        "run_dispatches": 0,
        "block_id": None,
    }

    assert _result_satisfies_replay_contract(result) is False

    result["run_dispatches"] = 1
    assert _result_satisfies_replay_contract(result) is True


def test_report_contract_requires_exact_retained_control_identity() -> None:
    result = {
        "call_id": "control:code_safety",
        "tool_name": "update_workflow",
        "staged_writes": 0,
        "run_dispatches": 0,
        "block_id": CREDENTIAL_SCOUT_BLOCK_ID,
    }

    assert _result_satisfies_replay_contract(result) is False

    result["block_id"] = CODE_SAFETY_BLOCK_ID
    assert _result_satisfies_replay_contract(result) is True
