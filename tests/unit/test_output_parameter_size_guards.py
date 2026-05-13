"""Tests for SKY-9779: three-layer guard against oversized JSON-column block outputs.

Covers the three helpers added by the fix:
  - ``truncate_oversized_jsonb_value`` (Layer 1: DB-write chokepoint)
  - ``_maybe_truncate_loop_outputs`` (Layer 2: loop accumulator OOM guard)
  - ``_trim_branch_evaluations`` / ``_cap_debug_field`` (Layer 3: DecisionBlock filter)
"""

from typing import Any
from unittest.mock import patch

from skyvern.forge.sdk.db.utils import truncate_oversized_jsonb_value
from skyvern.forge.sdk.workflow.models.block import (
    DECISION_BLOCK_FIELD_MAX_BYTES,
    _cap_debug_field,
    _maybe_truncate_loop_outputs,
    _trim_branch_evaluations,
)

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
