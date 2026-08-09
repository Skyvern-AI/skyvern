"""Guards against oversized JSON block outputs (SKY-9779 storage, SKY-13015 API reads).

- ``truncate_oversized_jsonb_value`` (Layer 1: DB-write chokepoint)
- ``_maybe_truncate_loop_outputs`` (Layer 2: loop accumulator OOM guard)
- ``_trim_branch_evaluations`` / ``_cap_debug_field`` (Layer 3: DecisionBlock filter)
- ``truncate_oversized_response_value`` (Layer 4: run-detail API read path)
"""

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.constants import SKYVERN_UI_USER_AGENT
from skyvern.forge.sdk.db.utils import _custom_json_serializer, truncate_oversized_jsonb_value
from skyvern.forge.sdk.routes import agent_protocol
from skyvern.forge.sdk.routes.trigger_type import caps_run_response_values
from skyvern.forge.sdk.schemas.task_v2 import Thought
from skyvern.forge.sdk.schemas.tasks import TaskStatus
from skyvern.forge.sdk.schemas.workflow_runs import WorkflowRunBlock
from skyvern.forge.sdk.workflow import service as workflow_service_module
from skyvern.forge.sdk.workflow.models.block import (
    DECISION_BLOCK_FIELD_MAX_BYTES,
    _cap_debug_field,
    _maybe_truncate_loop_outputs,
    _trim_branch_evaluations,
)
from skyvern.forge.sdk.workflow.models.workflow import (
    Workflow,
    WorkflowDefinition,
    WorkflowRun,
    WorkflowRunResponseBase,
    WorkflowRunStatus,
    WorkflowStatus,
)
from skyvern.forge.sdk.workflow.service import WorkflowService, truncate_oversized_response_value
from skyvern.schemas.runs import RunType
from skyvern.schemas.workflows import BlockType
from skyvern.services import run_service, task_v2_service
from skyvern.webeye.actions.action_types import ActionType
from skyvern.webeye.actions.actions import Action

# ---------- Layer 1: truncate_oversized_jsonb_value ----------


def test_truncate_oversized_jsonb_value_passes_small_value_through() -> None:
    value = {"a": 1, "b": [1, 2, 3]}
    assert truncate_oversized_jsonb_value(value, context={"workflow_run_id": "wr_x"}) is value


def test_truncate_oversized_jsonb_value_returns_marker_above_cap() -> None:
    # Patch the cap to a small value so the test stays cheap.
    cap = 64 * 1024
    big = {"blob": "x" * (cap + 1024)}
    with patch("skyvern.forge.sdk.db.utils.OUTPUT_PARAMETER_MAX_VALUE_BYTES", cap):
        result = truncate_oversized_jsonb_value(big, context={"workflow_run_id": "wr_x"})
    assert isinstance(result, dict)
    assert result["truncated"] is True
    assert result["reason"] == "exceeded_max_jsonb_value_size"
    assert result["limit_bytes"] == cap
    assert result["original_size_bytes"] > cap


def test_truncate_oversized_jsonb_value_fails_open_on_serialization_error() -> None:
    """Unserializable values must not raise — workflows are more important than warehouse syncs."""

    class _Unencodable:
        pass

    value: Any = {"u": _Unencodable()}
    with patch("skyvern.forge.sdk.db.utils.LOG") as log:
        result = truncate_oversized_jsonb_value(value, context={"workflow_run_id": "wr_x"})
    assert result is value
    log.warning.assert_called_once()


def test_truncate_oversized_jsonb_value_fast_path_skips_serialization_for_scalars() -> None:
    """Scalars and None should bypass the serializer entirely (claude-bot #3 nit)."""
    with patch("skyvern.forge.sdk.db.utils._custom_json_serializer") as serializer:
        for v in (None, True, False, 0, 42, 3.14, "small"):
            assert truncate_oversized_jsonb_value(v) is v
        serializer.assert_not_called()


# ---------- Layer 2: _maybe_truncate_loop_outputs ----------


def test_maybe_truncate_loop_outputs_no_op_below_cap() -> None:
    outputs: list[list[dict[str, Any]]] = [
        [{"loop_value": i, "output_parameter": None, "output_value": {"x": i}}] for i in range(3)
    ]
    snapshot = [list(entry) for entry in outputs]
    _maybe_truncate_loop_outputs(outputs, workflow_run_id="wr_x", output_parameter_id="op_x")
    assert outputs == snapshot


def test_maybe_truncate_loop_outputs_collapses_old_iterations_above_cap() -> None:
    # Patch cap to a small value so the fixture is cheap. Real cap is much higher.
    blob = "x" * (300 * 1024)  # 300 KiB per iteration
    outputs: list[list[dict[str, Any]]] = [
        [{"loop_value": i, "output_parameter": None, "output_value": {"blob": blob}}] for i in range(5)
    ]
    last = outputs[-1]
    with patch("skyvern.forge.sdk.workflow.models.block.OUTPUT_PARAMETER_MAX_VALUE_BYTES", 512 * 1024):
        _maybe_truncate_loop_outputs(outputs, workflow_run_id="wr_x", output_parameter_id="op_x")
    # Shape preserved: still list[list[dict]] with the canonical per-entry schema.
    assert len(outputs) == 2
    summary, tail = outputs
    assert tail is last
    assert isinstance(summary, list) and len(summary) == 1
    summary_entry = summary[0]
    assert set(summary_entry.keys()) == {"loop_value", "output_parameter", "output_value"}
    truncation = summary_entry["output_value"]
    assert truncation["truncated"] is True
    assert truncation["reason"] == "loop_output_size_exceeded"
    assert truncation["iterations_summarized_through"] == 4


def test_maybe_truncate_loop_outputs_fails_open_on_serialization_error() -> None:
    """If size measurement raises, we must not blow up the loop — fail-open with a warning."""
    outputs: list[list[dict[str, Any]]] = [[{"loop_value": None, "output_parameter": None, "output_value": {"a": 1}}]]
    with (
        patch("skyvern.forge.sdk.workflow.models.block.json.dumps", side_effect=RuntimeError("boom")),
        patch("skyvern.forge.sdk.workflow.models.block.LOG") as log,
    ):
        _maybe_truncate_loop_outputs(outputs, workflow_run_id="wr_x", output_parameter_id="op_x")
    # outputs untouched, structured warning emitted
    assert len(outputs) == 1
    log.warning.assert_called_once()


# ---------- Layer 3: _cap_debug_field + _trim_branch_evaluations ----------


def test_cap_debug_field_truncates_oversized_string_with_suffix() -> None:
    value = "y" * (DECISION_BLOCK_FIELD_MAX_BYTES + 500)
    capped = _cap_debug_field(value)
    assert isinstance(capped, str)
    assert capped.startswith("y" * 100)
    assert "[truncated 500 bytes]" in capped
    # Total length is the cap plus the suffix string — never the original.
    assert len(capped.encode("utf-8")) < len(value.encode("utf-8"))


def test_cap_debug_field_passes_short_string_through() -> None:
    value = "hello world"
    assert _cap_debug_field(value) is value


def test_cap_debug_field_passes_non_string_through() -> None:
    """LLM responses often come back as dicts; we don't try to cap them here — Layer 1 catches aggregates."""
    payload = {"reasoning": "x", "result": True}
    assert _cap_debug_field(payload) is payload


def test_trim_branch_evaluations_drops_rendered_expression_on_non_matched() -> None:
    evaluations = [
        {
            "branch_id": "b1",
            "branch_index": 0,
            "criteria_type": "jinja2_template",
            "original_expression": "{{ x }} == 1",
            "rendered_expression": "1 == 1",
            "result": True,
            "is_matched": True,
            "is_default": False,
            "next_block_label": "next",
            "error": None,
        },
        {
            "branch_id": "b2",
            "branch_index": 1,
            "criteria_type": "jinja2_template",
            "original_expression": "{{ x }} == 2",
            "rendered_expression": "1 == 2",
            "result": False,
            "is_matched": False,
            "is_default": False,
            "next_block_label": "other",
            "error": None,
        },
    ]
    trimmed = _trim_branch_evaluations(evaluations)
    assert trimmed is not None
    matched, unmatched = trimmed
    # Matched keeps rendered_expression (small, under cap).
    assert matched["rendered_expression"] == "1 == 1"
    # Non-matched has it dropped, all else preserved.
    assert "rendered_expression" not in unmatched
    for key in (
        "branch_id",
        "branch_index",
        "criteria_type",
        "original_expression",
        "result",
        "is_matched",
        "is_default",
        "next_block_label",
        "error",
    ):
        assert key in unmatched


def test_trim_branch_evaluations_caps_matched_branch_rendered_expression() -> None:
    big_rendered = "z" * (DECISION_BLOCK_FIELD_MAX_BYTES + 2048)
    evaluations = [
        {
            "branch_id": "b1",
            "branch_index": 0,
            "criteria_type": "jinja2_template",
            "original_expression": "{{ x }}",
            "rendered_expression": big_rendered,
            "result": True,
            "is_matched": True,
            "is_default": False,
            "next_block_label": "next",
            "error": None,
        },
    ]
    trimmed = _trim_branch_evaluations(evaluations)
    assert trimmed is not None
    matched = trimmed[0]
    rendered = matched["rendered_expression"]
    assert "[truncated 2048 bytes]" in rendered
    assert len(rendered.encode("utf-8")) < len(big_rendered.encode("utf-8"))


def test_trim_branch_evaluations_handles_empty_or_none() -> None:
    assert _trim_branch_evaluations(None) is None
    assert _trim_branch_evaluations([]) == []


def test_custom_json_serializer_strips_nul_in_default_ascii_mode() -> None:
    """PG text/jsonb cannot store NUL — default ensure_ascii=True emits the 6-char \\u0000 escape."""
    serialized = _custom_json_serializer({"key\x00with\x00nul": "value\x00with\x00nul", "nested": ["a\x00b"]})
    assert "\\u0000" not in serialized
    assert "\x00" not in serialized
    assert json.loads(serialized) == {"keywithnul": "valuewithnul", "nested": ["ab"]}


def test_custom_json_serializer_strips_nul_in_ensure_ascii_false_mode() -> None:
    """Same scrub when ensure_ascii=False emits literal NUL bytes instead of escapes."""
    serialized = _custom_json_serializer({"a": "hi\x00there"}, ensure_ascii=False)
    assert "\x00" not in serialized


def test_custom_json_serializer_passes_clean_payload_unchanged() -> None:
    serialized = _custom_json_serializer({"a": "clean", "b": [1, 2, 3]})
    assert serialized == '{"a": "clean", "b": [1, 2, 3]}'


def test_custom_json_serializer_preserves_literal_unicode_escape_strings() -> None:
    """Regression: only actual NUL bytes get stripped, not user strings that literally spell out an escape."""
    literal_six_chars = "\\u0000"
    payload = {"x": literal_six_chars, "y": "abc" + literal_six_chars + "def"}
    serialized = _custom_json_serializer(payload)
    assert json.loads(serialized) == payload


# ---------- Layer 4: truncate_oversized_response_value (API read path) ----------


def test_truncate_oversized_response_value_passes_small_value_through() -> None:
    value = {"rows": [[1, 2], [3, 4]]}
    assert truncate_oversized_response_value(value, workflow_run_id="wr_x") is value


def test_truncate_oversized_response_value_returns_marker_above_cap() -> None:
    cap = 64 * 1024
    big = ["x" * 1024 for _ in range(cap // 512)]
    with patch("skyvern.forge.sdk.workflow.service.RUN_RESPONSE_MAX_VALUE_BYTES", cap):
        result = truncate_oversized_response_value(big, workflow_run_id="wr_x", output_key="sheet_read_output")
    # Entries that fit are kept; the trailing marker is what makes the drop explicit.
    marker = result[-1]
    assert marker["truncated"] is True
    assert marker["reason"] == "exceeded_max_run_response_value_size"
    assert marker["limit_bytes"] == cap
    assert marker["original_size_bytes"] > cap
    assert marker["original_count"] == len(big)
    assert 0 < marker["kept_count"] < len(big)
    assert len(json.dumps(result, separators=(",", ":")).encode("utf-8")) <= cap


def test_truncate_oversized_response_value_fails_open_on_serialization_error() -> None:
    class _Unencodable:
        __slots__ = ()

        def __repr__(self) -> str:
            raise RuntimeError("boom")

    value: Any = {"u": _Unencodable()}
    with patch("skyvern.forge.sdk.workflow.service.LOG") as log:
        assert truncate_oversized_response_value(value, workflow_run_id="wr_x") is value
    log.warning.assert_called_once()


def test_truncate_oversized_response_value_fast_path_skips_serialization_for_scalars() -> None:
    with patch("skyvern.forge.sdk.workflow.service.json.dumps") as dumps:
        for v in (None, True, False, 0, 42, 3.14):
            assert truncate_oversized_response_value(v) is v
        dumps.assert_not_called()


@pytest.mark.asyncio
async def test_get_workflow_run_timeline_caps_oversized_block_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """A single sheet/file-read block output must not reach the timeline response (SKY-13015)."""
    now = datetime.now(timezone.utc)
    cap = 64 * 1024
    block = WorkflowRunBlock(
        workflow_run_block_id="wrb_1",
        workflow_run_id="wr_1",
        organization_id="o_1",
        block_type=BlockType.GOOGLE_SHEETS_READ,
        output={"values": ["x" * 1024 for _ in range(cap // 512)]},
        created_at=now,
        modified_at=now,
    )

    monkeypatch.setattr(
        workflow_service_module.app,
        "DATABASE",
        SimpleNamespace(
            observer=SimpleNamespace(get_workflow_run_blocks=AsyncMock(return_value=[block])),
            tasks=SimpleNamespace(get_tasks_actions=AsyncMock(return_value=[])),
        ),
    )
    monkeypatch.setattr(workflow_service_module, "RUN_RESPONSE_MAX_VALUE_BYTES", cap)

    timeline = await WorkflowService().get_workflow_run_timeline(
        workflow_run_id="wr_1", organization_id="o_1", cap_output_values=True
    )

    assert len(timeline) == 1
    output = timeline[0].block.output
    assert output["values"][-1]["truncated"] is True
    assert output["values"][-1]["reason"] == "exceeded_max_run_response_value_size"


def _oversized_value(cap: int) -> dict[str, Any]:
    return {"values": ["x" * 1024 for _ in range(cap // 512)]}


async def _build_status_response(
    monkeypatch: pytest.MonkeyPatch,
    *,
    cap: int,
    cap_output_values: bool,
    output_value: Any = None,
    refresh_output_urls: Any = None,
    failure_reason: str | None = None,
) -> WorkflowRunResponseBase:
    now = datetime.now(timezone.utc)
    workflow = Workflow(
        workflow_id="w_1",
        organization_id="o_1",
        title="T",
        workflow_permanent_id="wpid_1",
        version=1,
        is_saved_task=False,
        workflow_definition=WorkflowDefinition(parameters=[], blocks=[]),
        created_at=now,
        modified_at=now,
        status=WorkflowStatus.published,
    )
    workflow_run = WorkflowRun(
        workflow_run_id="wr_1",
        workflow_id="w_1",
        workflow_permanent_id="wpid_1",
        organization_id="o_1",
        status=WorkflowRunStatus.failed if failure_reason else WorkflowRunStatus.completed,
        failure_reason=failure_reason,
        created_at=now,
        modified_at=now,
    )
    output_parameter = SimpleNamespace(key="sheet_read_output")
    output = SimpleNamespace(value=_oversized_value(cap) if output_value is None else output_value)

    monkeypatch.setattr(
        workflow_service_module.app,
        "DATABASE",
        SimpleNamespace(
            workflows=SimpleNamespace(get_workflow_for_workflow_run=AsyncMock(return_value=workflow)),
            observer=SimpleNamespace(get_task_v2_by_workflow_run_id=AsyncMock(return_value=None)),
            tasks=SimpleNamespace(get_tasks_by_workflow_run_id=AsyncMock(return_value=[])),
            workflow_runs=SimpleNamespace(
                get_workflow_run_parameters=AsyncMock(return_value=[]),
                get_workflow_run_block_errors=AsyncMock(return_value=[]),
                get_workflow_run_retried_by=AsyncMock(return_value=None),
            ),
        ),
    )
    monkeypatch.setattr(workflow_service_module, "RUN_RESPONSE_MAX_VALUE_BYTES", cap)

    service = WorkflowService()
    monkeypatch.setattr(service, "get_workflow_run", AsyncMock(return_value=workflow_run))
    monkeypatch.setattr(service, "get_recent_workflow_screenshot_urls", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        service,
        "get_output_parameter_workflow_run_output_parameter_tuples",
        AsyncMock(return_value=[(output_parameter, output)]),
    )
    monkeypatch.setattr(service, "_fetch_recording_urls", AsyncMock(return_value=([], False)))
    monkeypatch.setattr(service, "_fetch_downloaded_files", AsyncMock(return_value=([], None)))
    # Identity by default: URL refresh is orthogonal to most of the cases under test.
    monkeypatch.setattr(
        service,
        "_refresh_output_urls",
        AsyncMock(side_effect=refresh_output_urls or (lambda value, **_: value)),
    )

    return await service.build_workflow_run_status_response(
        workflow_permanent_id="wpid_1",
        workflow_run_id="wr_1",
        organization_id="o_1",
        cap_output_values=cap_output_values,
    )


@pytest.mark.asyncio
async def test_status_response_caps_oversized_output_when_opted_in(monkeypatch: pytest.MonkeyPatch) -> None:
    cap = 64 * 1024
    response = await _build_status_response(monkeypatch, cap=cap, cap_output_values=True)
    assert response.outputs["sheet_read_output"]["values"][-1]["truncated"] is True


@pytest.mark.asyncio
async def test_status_response_leaves_output_intact_for_webhook_delivery(monkeypatch: pytest.MonkeyPatch) -> None:
    """Webhook delivery and replay must keep full fidelity; capping is opt-in."""
    cap = 64 * 1024
    response = await _build_status_response(monkeypatch, cap=cap, cap_output_values=False)
    assert response.outputs["sheet_read_output"] == _oversized_value(cap)


@pytest.mark.asyncio
async def test_status_response_caps_failure_reason_only_when_opted_in(monkeypatch: pytest.MonkeyPatch) -> None:
    cap = 4 * 1024
    failure_reason = "upstream response: " + "x" * (cap * 3)

    capped = await _build_status_response(
        monkeypatch,
        cap=cap,
        cap_output_values=True,
        failure_reason=failure_reason,
    )
    assert capped.failure_reason is not None
    assert capped.failure_reason.startswith("upstream response:")
    assert "[truncated" in capped.failure_reason
    assert len(json.dumps(capped.failure_reason).encode("utf-8")) <= cap

    uncapped = await _build_status_response(
        monkeypatch,
        cap=cap,
        cap_output_values=False,
        failure_reason=failure_reason,
    )
    assert uncapped.failure_reason == failure_reason


@pytest.mark.asyncio
async def test_task_run_response_caps_failure_reason_only_when_opted_in(monkeypatch: pytest.MonkeyPatch) -> None:
    cap = 4 * 1024
    failure_reason = "upstream response: " + "x" * (cap * 3)
    run = SimpleNamespace(run_id="tsk_1", task_run_type=RunType.task_v1)
    task_v1_response = MagicMock(
        task_id="tsk_1",
        status=TaskStatus.failed,
        extracted_information=None,
        failure_reason=failure_reason,
    )
    monkeypatch.setattr(
        run_service.app,
        "DATABASE",
        SimpleNamespace(tasks=SimpleNamespace(get_run=AsyncMock(return_value=run))),
    )
    monkeypatch.setattr(run_service.task_v1_service, "get_task_v1_response", AsyncMock(return_value=task_v1_response))
    monkeypatch.setattr(run_service, "TaskRunResponse", lambda **kwargs: SimpleNamespace(**kwargs))
    monkeypatch.setattr(run_service, "TaskRunRequest", lambda **kwargs: SimpleNamespace(**kwargs))
    monkeypatch.setattr(workflow_service_module, "RUN_RESPONSE_MAX_VALUE_BYTES", cap)

    capped = await run_service.get_run_response("tsk_1", organization_id="o_1", cap_output_values=True)
    assert capped is not None
    assert capped.failure_reason is not None
    assert capped.failure_reason.startswith("upstream response:")
    assert "[truncated" in capped.failure_reason
    assert len(json.dumps(capped.failure_reason).encode("utf-8")) <= cap


def test_truncate_oversized_response_value_measures_non_ascii_at_wire_size() -> None:
    """Default json.dumps escapes non-ASCII to \\uXXXX, over-counting CJK ~2x vs the UTF-8 wire form."""
    cap = 64 * 1024
    # Under the cap as UTF-8 (3 bytes/char), over it as escaped ASCII (6 bytes/char).
    value = {"text": "世" * 15000}
    assert len(json.dumps(value, ensure_ascii=False).encode("utf-8")) < cap
    assert len(json.dumps(value).encode("utf-8")) > cap
    with patch("skyvern.forge.sdk.workflow.service.RUN_RESPONSE_MAX_VALUE_BYTES", cap):
        assert truncate_oversized_response_value(value, workflow_run_id="wr_x") is value


def test_truncate_oversized_response_value_keeps_small_siblings_of_an_oversized_entry() -> None:
    """The UI reads downloaded_file_urls / workflow_run_id structurally — they must survive."""
    cap = 64 * 1024
    value = {
        "values": _oversized_value(cap)["values"],
        "downloaded_file_urls": ["https://example.com/a.pdf"],
        "workflow_run_id": "wr_child_1",
    }
    with patch("skyvern.forge.sdk.workflow.service.RUN_RESPONSE_MAX_VALUE_BYTES", cap):
        result = truncate_oversized_response_value(value, workflow_run_id="wr_x")
    assert result["downloaded_file_urls"] == ["https://example.com/a.pdf"]
    assert result["workflow_run_id"] == "wr_child_1"
    assert result["values"][-1]["truncated"] is True


def test_truncate_oversized_response_value_reserves_budget_for_siblings_of_a_large_collection() -> None:
    """A large child collection must not consume the parent value's entire budget."""
    cap = 64 * 1024
    value = {
        "task_output": ["x" * 1024 for _ in range(200)],
        "downloaded_file_urls": [f"https://example.com/{index}/{'a' * 96}" for index in range(12)],
        "task_screenshot_artifact_ids": [f"artifact_{index}_{'b' * 32}" for index in range(12)],
    }
    with patch("skyvern.forge.sdk.workflow.service.RUN_RESPONSE_MAX_VALUE_BYTES", cap):
        result = truncate_oversized_response_value(value, workflow_run_id="wr_x")

    assert result["downloaded_file_urls"] == value["downloaded_file_urls"]
    assert result["task_screenshot_artifact_ids"] == value["task_screenshot_artifact_ids"]
    assert result["task_output"][-1]["truncated"] is True
    assert len(json.dumps(result, separators=(",", ":")).encode("utf-8")) <= cap


def test_truncate_oversized_response_value_reserves_budget_for_later_list_elements() -> None:
    """The same reservation applies when a list's large first child has later metadata."""
    cap = 64 * 1024
    later_metadata = {
        "downloaded_file_urls": [f"https://example.com/{index}/{'a' * 96}" for index in range(12)],
        "task_screenshot_artifact_ids": [f"artifact_{index}_{'b' * 32}" for index in range(12)],
    }
    value = [["x" * 1024 for _ in range(200)], later_metadata]
    with patch("skyvern.forge.sdk.workflow.service.RUN_RESPONSE_MAX_VALUE_BYTES", cap):
        result = truncate_oversized_response_value(value, workflow_run_id="wr_x")

    assert result[1] == later_metadata
    assert result[0][-1]["truncated"] is True
    assert len(json.dumps(result, separators=(",", ":")).encode("utf-8")) <= cap


def test_truncate_oversized_response_value_marks_whole_value_when_no_single_entry_is_oversized() -> None:
    """Many medium siblings — trimming per-entry can't get under the cap, so mark the whole value."""
    cap = 64 * 1024
    value = {f"k{i}": "x" * (cap // 4) for i in range(8)}
    with patch("skyvern.forge.sdk.workflow.service.RUN_RESPONSE_MAX_VALUE_BYTES", cap):
        result = truncate_oversized_response_value(value, workflow_run_id="wr_x")
    marker = result[workflow_service_module.TRUNCATION_MARKER_KEY]
    assert marker["truncated"] is True
    assert marker["reason"] == "exceeded_max_run_response_value_size"
    # Some siblings survive rather than the whole dict collapsing.
    assert 0 < marker["kept_count"] < len(value)
    assert len(json.dumps(result, separators=(",", ":")).encode("utf-8")) <= cap


@pytest.mark.asyncio
async def test_status_response_keeps_extracted_information_from_an_oversized_loop_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A loop output over the cap still yields every iteration's extracted_information."""
    cap = 64 * 1024
    iterations = [{"raw_page": "x" * 2048, "extracted_information": [{"row": i}]} for i in range(60)]
    response = await _build_status_response(
        monkeypatch, cap=cap, cap_output_values=True, output_value={"iterations": iterations}
    )
    assert response.outputs["sheet_read_output"]["iterations"][-1]["truncated"] is True
    assert [entry["row"] for entry in response.outputs["extracted_information"]] == list(range(60))


def test_truncate_oversized_response_value_measures_at_compact_separator_size() -> None:
    """JSONResponse.render uses separators=(",", ":"); the defaults add ", "/": " (~12% wider)."""
    cap = 64 * 1024
    rows = [[f"cell{c}" for c in range(7)] for _ in range(1100)]
    wire = len(json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    padded = len(json.dumps(rows, ensure_ascii=False).encode("utf-8"))
    assert wire < cap < padded, "fixture must straddle the cap between the two encodings"
    with patch("skyvern.forge.sdk.workflow.service.RUN_RESPONSE_MAX_VALUE_BYTES", cap):
        assert truncate_oversized_response_value(rows, workflow_run_id="wr_x") is rows


def test_truncate_oversized_response_value_stops_descending_past_the_depth_cap() -> None:
    """Unbounded recursion re-serializes each subtree: 400 levels cost 1.27GB and ~2s."""
    cap = 64 * 1024
    value: Any = {"blob": "x" * (cap * 2)}
    for _ in range(50):
        value = {"n": value}

    calls = 0
    real_dumps = json.dumps

    def counting_dumps(*args: Any, **kwargs: Any) -> str:
        nonlocal calls
        calls += 1
        return real_dumps(*args, **kwargs)

    with (
        patch("skyvern.forge.sdk.workflow.service.RUN_RESPONSE_MAX_VALUE_BYTES", cap),
        patch("skyvern.forge.sdk.workflow.service.json.dumps", side_effect=counting_dumps),
    ):
        result = truncate_oversized_response_value(value, workflow_run_id="wr_x")

    # Depth cap of 4 => at most one measure + one trimmed-measure per level, plus leaves.
    assert calls <= 4 * (2 + 2) + 2, calls
    # Siblings are preserved down to the cap, then the remaining subtree is marked —
    # the 50-level tail below is never walked.
    marker = result["n"]["n"]["n"]["n"]
    assert marker["truncated"] is True


@pytest.mark.asyncio
async def test_get_workflow_run_timeline_caps_oversized_loop_values(monkeypatch: pytest.MonkeyPatch) -> None:
    """loop_values rides the same response and no write guard covers it."""
    now = datetime.now(timezone.utc)
    cap = 64 * 1024
    block = WorkflowRunBlock(
        workflow_run_block_id="wrb_1",
        workflow_run_id="wr_1",
        organization_id="o_1",
        block_type=BlockType.FOR_LOOP,
        loop_values=[{"row": i, "pad": "x" * 1024} for i in range(200)],
        created_at=now,
        modified_at=now,
    )
    monkeypatch.setattr(
        workflow_service_module.app,
        "DATABASE",
        SimpleNamespace(
            observer=SimpleNamespace(get_workflow_run_blocks=AsyncMock(return_value=[block])),
            tasks=SimpleNamespace(get_tasks_actions=AsyncMock(return_value=[])),
        ),
    )
    monkeypatch.setattr(workflow_service_module, "RUN_RESPONSE_MAX_VALUE_BYTES", cap)

    timeline = await WorkflowService().get_workflow_run_timeline(
        workflow_run_id="wr_1", organization_id="o_1", cap_output_values=True
    )

    loop_values = timeline[0].block.loop_values
    # Still a list, so the declared list[Any] | None contract holds for SDK callers.
    assert isinstance(loop_values, list)
    # 200/200, not 200/1: the run-detail page reads its iteration count off this length.
    assert len(loop_values) == 200
    assert loop_values[0] == workflow_service_module.LOOP_VALUE_TRUNCATED_PLACEHOLDER
    assert len(json.dumps(loop_values, separators=(",", ":")).encode("utf-8")) <= cap


@pytest.mark.asyncio
async def test_get_workflow_run_timeline_leaves_small_loop_values_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime.now(timezone.utc)
    block = WorkflowRunBlock(
        workflow_run_block_id="wrb_1",
        workflow_run_id="wr_1",
        organization_id="o_1",
        block_type=BlockType.FOR_LOOP,
        loop_values=["alpha", "beta"],
        created_at=now,
        modified_at=now,
    )
    monkeypatch.setattr(
        workflow_service_module.app,
        "DATABASE",
        SimpleNamespace(
            observer=SimpleNamespace(get_workflow_run_blocks=AsyncMock(return_value=[block])),
            tasks=SimpleNamespace(get_tasks_actions=AsyncMock(return_value=[])),
        ),
    )
    timeline = await WorkflowService().get_workflow_run_timeline(
        workflow_run_id="wr_1", organization_id="o_1", cap_output_values=True
    )
    assert timeline[0].block.loop_values == ["alpha", "beta"]


@pytest.mark.asyncio
async def test_status_response_recaps_after_url_refresh_expands_artifact_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_refresh_output_urls turns artifact IDs into presigned URLs (~25x), so re-cap after it."""
    cap = 64 * 1024
    small_with_ids = {"task_screenshot_artifact_ids": [f"a_{i}" for i in range(50)]}
    assert len(json.dumps(small_with_ids, separators=(",", ":")).encode("utf-8")) < cap

    # _refresh_output_urls receives the whole outputs dict and substitutes in place.
    def expand(outputs: Any, **_: Any) -> Any:
        outputs["sheet_read_output"]["task_screenshots"] = ["https://example.com/" + "s" * 2048 for _ in range(50)]
        return outputs

    response = await _build_status_response(
        monkeypatch,
        cap=cap,
        cap_output_values=True,
        output_value=small_with_ids,
        refresh_output_urls=expand,
    )
    assert response.outputs["sheet_read_output"]["task_screenshots"][-1]["truncated"] is True


def test_truncate_oversized_response_value_preserves_list_envelope() -> None:
    """A ForLoopBlock output parameter is list[list[dict]]; collapsing it loses every iteration."""
    cap = 64 * 1024
    iterations: list[list[dict[str, Any]]] = [
        [{"output_value": {"downloaded_file_urls": [f"https://example.com/{i}.pdf"]}}] for i in range(3)
    ]
    iterations[1][0]["output_value"]["blob"] = "x" * (cap * 2)
    with patch("skyvern.forge.sdk.workflow.service.RUN_RESPONSE_MAX_VALUE_BYTES", cap):
        result = truncate_oversized_response_value(iterations, workflow_run_id="wr_x")
    assert isinstance(result, list) and len(result) == 3
    # Untouched iterations keep their nested per-block data.
    assert result[0][0]["output_value"]["downloaded_file_urls"] == ["https://example.com/0.pdf"]
    assert result[2][0]["output_value"]["downloaded_file_urls"] == ["https://example.com/2.pdf"]
    assert result[1][0]["output_value"]["blob"]["truncated"] is True


def test_truncate_oversized_response_value_preserves_nested_loop_output_envelopes() -> None:
    cap = 4 * 1024
    loop_output = [
        [
            {
                "loop_value": {"row": 1},
                "output_parameter": {"key": "child_output"},
                "output_value": {
                    "extracted_information": {
                        "answer": "keep me",
                        "raw": "x" * (cap * 3),
                    },
                    "downloaded_file_urls": ["https://files.invalid/result.pdf"],
                    "workflow_run_id": "wr_child",
                    "outputs": {
                        "nested_trigger_output": {
                            "workflow_run_id": "wr_grandchild",
                            "downloaded_file_urls": ["https://files.invalid/child.pdf"],
                            "raw": "y" * (cap * 3),
                        }
                    },
                },
            }
        ]
    ]

    with patch("skyvern.forge.sdk.workflow.service.RUN_RESPONSE_MAX_VALUE_BYTES", cap):
        result = truncate_oversized_response_value(loop_output, workflow_run_id="wr_x")

    output_value = result[0][0]["output_value"]
    assert output_value["extracted_information"]["answer"] == "keep me"
    assert output_value["extracted_information"]["raw"]["truncated"] is True
    assert output_value["downloaded_file_urls"] == ["https://files.invalid/result.pdf"]
    assert output_value["workflow_run_id"] == "wr_child"
    nested_output = output_value["outputs"]["nested_trigger_output"]
    assert nested_output["workflow_run_id"] == "wr_grandchild"
    assert nested_output["downloaded_file_urls"] == ["https://files.invalid/child.pdf"]
    assert nested_output["raw"]["truncated"] is True
    assert len(json.dumps(result, separators=(",", ":")).encode("utf-8")) <= cap


def test_truncate_oversized_response_value_reserves_loop_envelope_overhead() -> None:
    """Small loop metadata must not crowd an otherwise-under-cap output_value out."""
    cap = 4 * 1024
    entry = {
        "loop_value": {"row": 1, "metadata": "m" * 200},
        "output_parameter": {"key": "child_output", "description": "p" * 200},
        "output_value": {"answer": "keep me", "blob": "x" * 3600},
    }
    value = [[entry]]
    assert len(json.dumps(entry["output_value"], separators=(",", ":")).encode("utf-8")) < cap
    assert len(json.dumps(value, separators=(",", ":")).encode("utf-8")) > cap

    with patch("skyvern.forge.sdk.workflow.service.RUN_RESPONSE_MAX_VALUE_BYTES", cap):
        result = truncate_oversized_response_value(value, workflow_run_id="wr_x")

    capped_entry = result[0][0]
    assert capped_entry["loop_value"]["row"] == 1
    assert capped_entry["output_parameter"]["key"] == "child_output"
    assert capped_entry["output_value"]["answer"] == "keep me"
    assert capped_entry["output_value"]["blob"]["truncated"] is True
    assert len(json.dumps(result, separators=(",", ":")).encode("utf-8")) <= cap


def test_truncate_oversized_response_value_bounds_nested_loop_envelope_depth() -> None:
    cap = 4 * 1024
    nested_output: dict[str, Any] = {"blob": "x" * (cap * 2)}
    for index in range(12):
        nested_output = {
            "loop_value": index,
            "output_parameter": {"key": f"nested_{index}"},
            "output_value": nested_output,
        }

    with patch("skyvern.forge.sdk.workflow.service.RUN_RESPONSE_MAX_VALUE_BYTES", cap):
        result = truncate_oversized_response_value([[nested_output]], workflow_run_id="wr_x")

    assert json.dumps(result, separators=(",", ":")).count('"output_value"') < 12
    assert '"truncated":true' in json.dumps(result, separators=(",", ":"))


@pytest.mark.asyncio
async def test_get_workflow_run_timeline_caps_navigation_payload_and_data_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both are unbounded jsonb copied from the task and ride the same response."""
    now = datetime.now(timezone.utc)
    cap = 64 * 1024
    block = WorkflowRunBlock(
        workflow_run_block_id="wrb_1",
        workflow_run_id="wr_1",
        organization_id="o_1",
        block_type=BlockType.TASK,
        navigation_payload={"rows": ["x" * 1024 for _ in range(200)]},
        data_schema={"rows": ["y" * 1024 for _ in range(200)]},
        created_at=now,
        modified_at=now,
    )
    monkeypatch.setattr(
        workflow_service_module.app,
        "DATABASE",
        SimpleNamespace(
            observer=SimpleNamespace(get_workflow_run_blocks=AsyncMock(return_value=[block])),
            tasks=SimpleNamespace(get_tasks_actions=AsyncMock(return_value=[])),
        ),
    )
    monkeypatch.setattr(workflow_service_module, "RUN_RESPONSE_MAX_VALUE_BYTES", cap)

    timeline = await WorkflowService().get_workflow_run_timeline(
        workflow_run_id="wr_1", organization_id="o_1", cap_output_values=True
    )

    assert timeline[0].block.navigation_payload["rows"][-1]["truncated"] is True
    assert timeline[0].block.data_schema["rows"][-1]["truncated"] is True


def test_truncate_oversized_response_value_bails_out_before_serializing_every_element() -> None:
    """Many-small-rows can never fit; stop once the kept elements alone clear the cap."""
    cap = 64 * 1024
    rows = [{"row": i, "pad": "x" * 512} for i in range(4000)]

    calls = 0
    real_dumps = json.dumps

    def counting_dumps(*args: Any, **kwargs: Any) -> str:
        nonlocal calls
        calls += 1
        return real_dumps(*args, **kwargs)

    with (
        patch("skyvern.forge.sdk.workflow.service.RUN_RESPONSE_MAX_VALUE_BYTES", cap),
        patch("skyvern.forge.sdk.workflow.service.json.dumps", side_effect=counting_dumps),
    ):
        result = truncate_oversized_response_value(rows, workflow_run_id="wr_x")

    assert result[-1]["truncated"] is True
    # Bails after ~cap/512 elements rather than walking all 4000 and re-serializing the copy.
    assert calls < 500, calls


def test_truncate_oversized_response_value_counts_dict_keys_and_separators() -> None:
    """Summing child values alone under-counts a key-heavy dict by ~half."""
    cap = 64 * 1024
    # Children total under the cap; keys plus separators push the real payload over it.
    value = {f"{'k' * 40}{i:04d}": "v" * 20 for i in range(1200)}
    children_only = sum(len(json.dumps(v, separators=(",", ":")).encode("utf-8")) for v in value.values())
    real = len(json.dumps(value, separators=(",", ":")).encode("utf-8"))
    assert children_only < cap < real, (children_only, cap, real)

    with patch("skyvern.forge.sdk.workflow.service.RUN_RESPONSE_MAX_VALUE_BYTES", cap):
        result = truncate_oversized_response_value(value, workflow_run_id="wr_x")

    # Whatever comes back must actually fit on the wire.
    assert len(json.dumps(result, separators=(",", ":")).encode("utf-8")) <= cap


@pytest.mark.asyncio
async def test_get_workflow_run_timeline_caps_current_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """A loop child block persists str(loop_over_value); a multi-MB item lands here in full."""
    now = datetime.now(timezone.utc)
    cap = 4 * 1024
    block = WorkflowRunBlock(
        workflow_run_block_id="wrb_1",
        workflow_run_id="wr_1",
        organization_id="o_1",
        block_type=BlockType.TASK,
        current_value="z" * (cap * 4),
        created_at=now,
        modified_at=now,
    )
    monkeypatch.setattr(
        workflow_service_module.app,
        "DATABASE",
        SimpleNamespace(
            observer=SimpleNamespace(get_workflow_run_blocks=AsyncMock(return_value=[block])),
            tasks=SimpleNamespace(get_tasks_actions=AsyncMock(return_value=[])),
        ),
    )
    monkeypatch.setattr(workflow_service_module, "RUN_RESPONSE_MAX_VALUE_BYTES", cap)

    timeline = await WorkflowService().get_workflow_run_timeline(
        workflow_run_id="wr_1", organization_id="o_1", cap_output_values=True
    )

    current_value = timeline[0].block.current_value
    # Still a str, so the declared str | None contract holds, and it identifies the iteration.
    assert isinstance(current_value, str)
    assert current_value.startswith("zzz")
    assert "[truncated" in current_value


# ---------- Which surfaces bound their output values ----------


def test_only_the_app_user_agent_caps_run_response_values() -> None:
    """The app renders outputs with JSON.stringify on the main thread; nothing else needs a bound."""
    assert caps_run_response_values(SKYVERN_UI_USER_AGENT) is True
    # SDK/webhook/replay callers and MCP (bounded at its own ceiling) keep full fidelity.
    assert caps_run_response_values(None) is False
    assert caps_run_response_values("skyvern-mcp") is False
    assert caps_run_response_values("python-httpx/0.27") is False


@pytest.mark.asyncio
async def test_get_workflow_run_timeline_leaves_blocks_untouched_for_api_callers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An SDK caller on get_run_timeline gets the stored block output in full."""
    now = datetime.now(timezone.utc)
    cap = 64 * 1024
    stored_output = {"values": ["x" * 1024 for _ in range(200)]}
    block = WorkflowRunBlock(
        workflow_run_block_id="wrb_1",
        workflow_run_id="wr_1",
        organization_id="o_1",
        block_type=BlockType.GOOGLE_SHEETS_READ,
        output=stored_output,
        created_at=now,
        modified_at=now,
    )
    monkeypatch.setattr(
        workflow_service_module.app,
        "DATABASE",
        SimpleNamespace(
            observer=SimpleNamespace(get_workflow_run_blocks=AsyncMock(return_value=[block])),
            tasks=SimpleNamespace(get_tasks_actions=AsyncMock(return_value=[])),
        ),
    )
    monkeypatch.setattr(workflow_service_module, "RUN_RESPONSE_MAX_VALUE_BYTES", cap)

    timeline = await WorkflowService().get_workflow_run_timeline(workflow_run_id="wr_1", organization_id="o_1")

    assert timeline[0].block.output == stored_output


def test_capped_loop_values_keeps_placeholder_list_under_the_cap() -> None:
    """At ~150k iterations even one placeholder each overruns the cap."""
    cap = 64 * 1024
    placeholder_cost = len(json.dumps(workflow_service_module.LOOP_VALUE_TRUNCATED_PLACEHOLDER)) + 1
    count = (cap // placeholder_cost) + 500
    loop_values = [{"row": i, "pad": "x" * 64} for i in range(count)]

    with patch("skyvern.forge.sdk.workflow.service.RUN_RESPONSE_MAX_VALUE_BYTES", cap):
        result = workflow_service_module._capped_loop_values(loop_values, workflow_run_id="wr_x")

    assert result is not None
    assert len(json.dumps(result, separators=(",", ":")).encode("utf-8")) <= cap
    # The count moves into bounded metadata once it can no longer be the list length.
    assert result[0]["original_count"] == count


def test_capped_response_text_measures_at_json_wire_size() -> None:
    """JSON escaping inflates quotes and control chars; a raw byte count understates it ~2x."""
    cap = 64 * 1024
    # Under the cap as raw UTF-8, over it once JSON escapes every character.
    value = '"' * (cap - 1000)
    assert len(value.encode("utf-8")) < cap < len(json.dumps(value).encode("utf-8"))

    with patch("skyvern.forge.sdk.workflow.service.RUN_RESPONSE_MAX_VALUE_BYTES", cap):
        result = workflow_service_module.truncate_oversized_response_text(value)

    assert isinstance(result, str)
    assert len(json.dumps(result).encode("utf-8")) <= cap
    assert "[truncated" in result


def test_truncate_oversized_response_value_keeps_rows_from_a_marginally_oversized_table() -> None:
    """A table just over the cap must still render, not vanish — the freeze was at 16.6MB."""
    cap = 64 * 1024
    row = [f"cell{c}" for c in range(7)]
    # Size the table a few percent over the cap, which is the case that used to vanish.
    per_row = len(json.dumps(row, separators=(",", ":")).encode("utf-8")) + 1
    count = int(cap / per_row * 1.05)
    rows = [list(row) for _ in range(count)]
    assert len(json.dumps(rows, separators=(",", ":")).encode("utf-8")) > cap

    with patch("skyvern.forge.sdk.workflow.service.RUN_RESPONSE_MAX_VALUE_BYTES", cap):
        result = truncate_oversized_response_value(rows, workflow_run_id="wr_x")

    marker = result[-1]
    assert marker["original_count"] == count
    # The overwhelming majority of rows survive rather than the table collapsing.
    assert marker["kept_count"] > count * 0.9
    assert result[0] == rows[0]
    assert len(json.dumps(result, separators=(",", ":")).encode("utf-8")) <= cap


def test_truncate_oversized_response_value_never_drops_entries_without_a_marker() -> None:
    """The loop stops a marker's width below the cap; that gap must not read as 'complete'."""
    cap = 64 * 1024
    for row_size in (256, 512, 1024, 2048):
        rows = [{"pad": "x" * row_size} for _ in range(400)]
        with patch("skyvern.forge.sdk.workflow.service.RUN_RESPONSE_MAX_VALUE_BYTES", cap):
            result = truncate_oversized_response_value(rows, workflow_run_id="wr_x")
        if isinstance(result, list) and len(result) < len(rows):
            assert result[-1].get("truncated") is True, f"silent drop at row_size={row_size}"


@pytest.mark.asyncio
async def test_get_task_v2_caps_output_for_the_app_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """RunRouter resolves a tsk_v2_* URL through this endpoint before redirecting."""
    now = datetime.now(timezone.utc)
    cap = 64 * 1024
    stored_output = {"rows": ["x" * 1024 for _ in range(200)]}
    task_v2 = SimpleNamespace(
        observer_cruise_id="tsk_v2_1",
        output=stored_output,
        model_copy=lambda update: SimpleNamespace(
            observer_cruise_id="tsk_v2_1",
            output=update["output"],
            model_dump=lambda by_alias: {"output": update["output"]},
        ),
        model_dump=lambda by_alias: {"output": stored_output},
    )
    monkeypatch.setattr(agent_protocol.task_v2_service, "get_task_v2", AsyncMock(return_value=task_v2), raising=False)
    monkeypatch.setattr(workflow_service_module, "RUN_RESPONSE_MAX_VALUE_BYTES", cap)
    organization = SimpleNamespace(organization_id="o_1")
    assert now is not None

    from_app = await agent_protocol.get_task_v2(
        task_id="tsk_v2_1", organization=organization, x_user_agent=SKYVERN_UI_USER_AGENT
    )
    assert from_app["output"]["rows"][-1]["truncated"] is True

    from_sdk = await agent_protocol.get_task_v2(task_id="tsk_v2_1", organization=organization, x_user_agent=None)
    assert from_sdk["output"] == stored_output


@pytest.mark.asyncio
async def test_get_task_v1_caps_extracted_information_for_the_app_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tsk_* run URL reaches this direct task-v1 endpoint before the UI renders it."""
    cap = 64 * 1024
    stored_output = {"rows": ["x" * 1024 for _ in range(200)]}

    def model_copy(update: dict[str, Any]) -> SimpleNamespace:
        return SimpleNamespace(
            task_id="tsk_1",
            extracted_information=update["extracted_information"],
            failure_reason=update["failure_reason"],
        )

    task_response = SimpleNamespace(
        task_id="tsk_1",
        extracted_information=stored_output,
        failure_reason="f" * (cap * 2),
        model_copy=model_copy,
    )
    monkeypatch.setattr(
        agent_protocol.task_v1_service,
        "get_task_v1_response",
        AsyncMock(return_value=task_response),
    )
    monkeypatch.setattr(agent_protocol.analytics, "capture", MagicMock())
    monkeypatch.setattr(workflow_service_module, "RUN_RESPONSE_MAX_VALUE_BYTES", cap)
    organization = SimpleNamespace(organization_id="o_1")

    from_app = await agent_protocol.get_task_v1(
        task_id="tsk_1", current_org=organization, x_user_agent=SKYVERN_UI_USER_AGENT
    )
    assert from_app.extracted_information["rows"][-1]["truncated"] is True
    assert from_app.failure_reason.endswith("chars]")
    assert len(json.dumps(from_app.failure_reason).encode("utf-8")) <= cap

    from_sdk = await agent_protocol.get_task_v1(task_id="tsk_1", current_org=organization, x_user_agent=None)
    assert from_sdk is task_response
    assert from_sdk.extracted_information == stored_output
    assert from_sdk.failure_reason == task_response.failure_reason


# Fields whose type could hold unbounded content but which are bounded by construction.
_EXEMPT_BLOCK_FIELDS = {
    "workflow_run_block_id",
    "block_workflow_run_id",
    "workflow_run_id",
    "organization_id",
    "parent_workflow_run_block_id",
    "task_id",
    "label",
    "status",
    "executed_branch_id",
    "executed_branch_next_block",
    "error_codes",  # enum codes
    "loop_values",  # capped separately, count-preserving
    "actions",  # capped per-action as they are hydrated
    "script_run",  # fixed-shape record
}


def test_timeline_block_field_coverage() -> None:
    """A new unbounded field on the model must be capped or explicitly exempted.

    This guard exists because the cap was extended one reviewer comment at a time across
    several rounds; without it the next field added is silently uncapped.
    """
    capped = (
        set(workflow_service_module.UNBOUNDED_BLOCK_JSON_FIELDS)
        | set(workflow_service_module.UNBOUNDED_BLOCK_TEXT_FIELDS)
        | set(workflow_service_module.UNBOUNDED_BLOCK_TEXT_LIST_FIELDS)
    )
    unbounded_types = set()
    for name, field in WorkflowRunBlock.model_fields.items():
        annotation = str(field.annotation)
        if "str" in annotation or "dict" in annotation or "list" in annotation or "Any" in annotation:
            unbounded_types.add(name)

    uncovered = unbounded_types - capped - _EXEMPT_BLOCK_FIELDS
    assert not uncovered, (
        f"WorkflowRunBlock fields can hold unbounded content but are neither capped nor exempt: "
        f"{sorted(uncovered)}. Add them to the matching UNBOUNDED_BLOCK field group, "
        f"or to _EXEMPT_BLOCK_FIELDS with a reason."
    )
    # Guard against the lists drifting to name fields that no longer exist.
    assert not (capped - set(WorkflowRunBlock.model_fields)), "capped list names a non-existent field"


@pytest.mark.asyncio
async def test_get_workflow_run_timeline_caps_unbounded_text_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prompts, email bodies and human-interaction instructions ride the same response."""
    now = datetime.now(timezone.utc)
    cap = 4 * 1024
    block = WorkflowRunBlock(
        workflow_run_block_id="wrb_1",
        workflow_run_id="wr_1",
        organization_id="o_1",
        block_type=BlockType.TASK,
        prompt="p" * (cap * 3),
        body="b" * (cap * 3),
        instructions="i" * (cap * 3),
        failure_reason="f" * (cap * 3),
        created_at=now,
        modified_at=now,
    )
    monkeypatch.setattr(
        workflow_service_module.app,
        "DATABASE",
        SimpleNamespace(
            observer=SimpleNamespace(get_workflow_run_blocks=AsyncMock(return_value=[block])),
            tasks=SimpleNamespace(get_tasks_actions=AsyncMock(return_value=[])),
        ),
    )
    monkeypatch.setattr(workflow_service_module, "RUN_RESPONSE_MAX_VALUE_BYTES", cap)

    timeline = await WorkflowService().get_workflow_run_timeline(
        workflow_run_id="wr_1", organization_id="o_1", cap_output_values=True
    )

    capped_block = timeline[0].block
    for field in ("prompt", "body", "instructions", "failure_reason"):
        value = getattr(capped_block, field)
        assert isinstance(value, str), field
        assert "[truncated" in value, field
        assert len(json.dumps(value).encode("utf-8")) <= cap, field


def test_capped_text_list_keeps_every_element_a_string() -> None:
    """recipients/attachments are list[str]; a marker dict inside them breaks the type."""
    cap = 4 * 1024
    values = ["a" * (cap * 2) + "@example.com", "small@example.com"]
    with patch("skyvern.forge.sdk.workflow.service.RUN_RESPONSE_MAX_VALUE_BYTES", cap):
        result = workflow_service_module._capped_text_list(values, workflow_run_id="wr_x")
    assert result is not None
    assert all(isinstance(v, str) for v in result), [type(v).__name__ for v in result]
    assert len(json.dumps(result, separators=(",", ":")).encode("utf-8")) <= cap


def test_capped_text_list_marker_is_a_string_when_many_entries_drop() -> None:
    cap = 4 * 1024
    values = [f"{'x' * 200}{i}@example.com" for i in range(200)]
    with patch("skyvern.forge.sdk.workflow.service.RUN_RESPONSE_MAX_VALUE_BYTES", cap):
        result = workflow_service_module._capped_text_list(values, workflow_run_id="wr_x")
    assert result is not None
    assert all(isinstance(v, str) for v in result)
    assert "truncated" in result[-1]
    assert len(json.dumps(result, separators=(",", ":")).encode("utf-8")) <= cap


def test_timeline_block_list_fields_never_receive_a_marker_dict() -> None:
    """Regression: the JSON trim would have put a dict into these typed list[str] fields."""
    for field in workflow_service_module.UNBOUNDED_BLOCK_TEXT_LIST_FIELDS:
        annotation = str(WorkflowRunBlock.model_fields[field].annotation)
        assert "str" in annotation and "dict" not in annotation, f"{field}: {annotation}"
        assert field not in workflow_service_module.UNBOUNDED_BLOCK_JSON_FIELDS


@pytest.mark.asyncio
async def test_get_workflow_run_timeline_caps_action_text(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime.now(timezone.utc)
    cap = 4 * 1024
    block = WorkflowRunBlock(
        workflow_run_block_id="wrb_1",
        workflow_run_id="wr_1",
        organization_id="o_1",
        block_type=BlockType.TASK,
        task_id="tsk_1",
        created_at=now,
        modified_at=now,
    )
    action = Action(
        action_type=ActionType.INPUT_TEXT,
        task_id="tsk_1",
        response="r" * (cap * 3),
        text="t" * (cap * 3),
    )
    monkeypatch.setattr(
        workflow_service_module.app,
        "DATABASE",
        SimpleNamespace(
            observer=SimpleNamespace(get_workflow_run_blocks=AsyncMock(return_value=[block])),
            tasks=SimpleNamespace(get_tasks_actions=AsyncMock(return_value=[action])),
        ),
    )
    monkeypatch.setattr(workflow_service_module, "RUN_RESPONSE_MAX_VALUE_BYTES", cap)

    timeline = await WorkflowService().get_workflow_run_timeline(
        workflow_run_id="wr_1", organization_id="o_1", cap_output_values=True
    )

    capped_action = timeline[0].block.actions[0]
    for field in ("response", "text"):
        value = getattr(capped_action, field)
        assert isinstance(value, str), field
        assert "[truncated" in value, field
        assert len(json.dumps(value).encode("utf-8")) <= cap, field


@pytest.mark.asyncio
async def test_get_thought_timelines_caps_text_fields_when_opted_in(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime.now(timezone.utc)
    cap = 4 * 1024
    oversized_text = "t" * (cap * 3)
    thought = Thought(
        thought_id="th_1",
        task_id="tsk_v2_1",
        organization_id="o_1",
        **dict.fromkeys(("user_input", "observation", "thought", "answer"), oversized_text),
        output={"value": "o" * (cap * 3)},
        created_at=now,
        modified_at=now,
    )
    monkeypatch.setattr(
        task_v2_service.app,
        "DATABASE",
        SimpleNamespace(observer=SimpleNamespace(get_thoughts=AsyncMock(return_value=[thought]))),
    )
    monkeypatch.setattr(workflow_service_module, "RUN_RESPONSE_MAX_VALUE_BYTES", cap)

    capped = await task_v2_service.get_thought_timelines(
        task_v2_id="tsk_v2_1", organization_id="o_1", cap_output_values=True
    )
    capped_thought = capped[0].thought
    assert capped_thought is not None
    for field in ("user_input", "observation", "thought", "answer"):
        value = getattr(capped_thought, field)
        assert isinstance(value, str) and "[truncated" in value, field
        assert len(json.dumps(value).encode("utf-8")) <= cap, field
    assert capped_thought.output["value"]["truncated"] is True


@pytest.mark.asyncio
async def test_get_workflow_run_timeline_preserves_string_list_field_types(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(timezone.utc)
    cap = 4 * 1024
    block = WorkflowRunBlock(
        workflow_run_block_id="wrb_1",
        workflow_run_id="wr_1",
        organization_id="o_1",
        block_type=BlockType.SEND_EMAIL,
        recipients=[f"recipient-{i}@example.invalid" for i in range(500)],
        attachments=[f"https://files.invalid/{i}/attachment.pdf" for i in range(500)],
        created_at=now,
        modified_at=now,
    )
    monkeypatch.setattr(
        workflow_service_module.app,
        "DATABASE",
        SimpleNamespace(
            observer=SimpleNamespace(get_workflow_run_blocks=AsyncMock(return_value=[block])),
            tasks=SimpleNamespace(get_tasks_actions=AsyncMock(return_value=[])),
        ),
    )
    monkeypatch.setattr(workflow_service_module, "RUN_RESPONSE_MAX_VALUE_BYTES", cap)

    timeline = await WorkflowService().get_workflow_run_timeline(
        workflow_run_id="wr_1", organization_id="o_1", cap_output_values=True
    )

    capped_block = timeline[0].block
    for field in ("recipients", "attachments"):
        values = getattr(capped_block, field)
        assert isinstance(values, list), field
        assert all(isinstance(value, str) for value in values), field
        assert "[truncated" in values[-1], field
        assert len(json.dumps(values, separators=(",", ":")).encode("utf-8")) <= cap, field
