"""Workflow-aware response selection for the MCP response transformation hook.

The workflow module owns serialization. This module only decides when an
already-serialized workflow response needs compaction and reuses the workflow
summary helpers for large run outputs.
"""

from __future__ import annotations

from typing import Any, Literal

from .response_distillation import TransformResult, TransformTier, distill_value

WorkflowToolName = Literal["skyvern_workflow_run", "skyvern_workflow_status"]
_OUTPUT_FIELDS = ("output", "outputs", "extracted_information")


def _passthrough(value: Any, reason: str) -> TransformResult[Any]:
    return TransformResult(
        value=value,
        tier=TransformTier.PASSTHROUGH,
        complete=True,
        fallback_reason=reason,
    )


def _field_exceeds_generic_threshold(field: str, value: Any) -> bool:
    """Apply the generic compactor at the same nesting depth as result data.

    Testing one field at a time avoids changing the established workflow
    summary merely because its data mapping contains several selected fields.
    """
    probe = distill_value({"data": {field: value}})
    return probe.tier is not TransformTier.PASSTHROUGH and not probe.complete


def _compact_selected_field(field: str, value: Any) -> Any:
    probe = distill_value({"data": {field: value}})
    if not isinstance(probe.value, dict):
        return value
    data = probe.value.get("data")
    if not isinstance(data, dict) or field not in data:
        return value
    return data[field]


def _recovery_marker(run_id: Any, reason: str) -> dict[str, Any]:
    if isinstance(run_id, str) and run_id:
        recovery_hint = (
            f"Call skyvern_workflow_status(run_id={run_id!r}, verbosity='full') to retrieve the full output."
        )
    else:
        recovery_hint = (
            "Call skyvern_workflow_status(run_id=<returned run_id>, verbosity='full') to retrieve the full output."
        )
    return {
        "complete": False,
        "tier": TransformTier.STRUCTURED.value,
        "recovery_hint": recovery_hint,
        "fallback_reason": reason,
    }


def _format_status_response(response: dict[str, Any], data: dict[str, Any]) -> TransformResult[Any]:
    compacted_data: dict[str, Any] | None = None
    for field, value in data.items():
        if not _field_exceeds_generic_threshold(field, value):
            continue
        if compacted_data is None:
            compacted_data = dict(data)
        compacted_data[field] = _compact_selected_field(field, value)

    if compacted_data is None:
        return _passthrough(response, "workflow_status_summary_within_threshold")

    formatted = dict(response)
    formatted["data"] = compacted_data
    # The run-specific marker survives the central completeness pass while the
    # incomplete provenance triggers anchor recovery for omitted fields.
    formatted["_response_distillation"] = _recovery_marker(
        compacted_data.get("run_id"), "workflow_status_field_summarized"
    )
    return TransformResult(
        value=formatted,
        tier=TransformTier.STRUCTURED,
        complete=False,
        fallback_reason="workflow_status_field_summarized",
        owns_completeness_marker=True,
    )


def _format_run_response(response: dict[str, Any], data: dict[str, Any]) -> TransformResult[Any]:
    large_fields = [
        field for field in _OUTPUT_FIELDS if field in data and _field_exceeds_generic_threshold(field, data[field])
    ]
    if not large_fields:
        return _passthrough(response, "workflow_run_output_within_threshold")

    # Lazy import avoids a module cycle while keeping the workflow serializer
    # and its semantic summary vocabulary in one place.
    from .workflow import _summarize_artifacts, _summarize_output_value

    compacted_data = dict(data)
    for field in large_fields:
        value = compacted_data.pop(field)
        output_summary, output_stats = _summarize_output_value(value)
        summary_field = "output_summary" if field in {"output", "outputs"} else "extracted_information_summary"
        compacted_data[summary_field] = output_summary
        if field in {"output", "outputs"}:
            compacted_data["artifact_summary"] = _summarize_artifacts(data, output_stats)

    formatted = dict(response)
    formatted["data"] = compacted_data
    formatted["_response_distillation"] = _recovery_marker(
        compacted_data.get("run_id"), "workflow_run_output_summarized"
    )
    return TransformResult(
        value=formatted,
        tier=TransformTier.STRUCTURED,
        complete=False,
        fallback_reason="workflow_run_output_summarized",
        owns_completeness_marker=True,
    )


def format_workflow_response(
    response: Any,
    *,
    tool_name: WorkflowToolName | None = None,
) -> TransformResult[Any]:
    """Select workflow response fields before generic response compaction.

    ``tool_name`` is supplied by each workflow tool because concise/FastMCP
    envelopes intentionally omit the ``action`` field. If omitted, the verbose
    result action is used when available.
    """
    if not isinstance(response, dict):
        return _passthrough(response, "workflow_response_not_mapping")

    selected_tool = tool_name or response.get("action")
    if selected_tool not in {"skyvern_workflow_run", "skyvern_workflow_status"}:
        return _passthrough(response, "not_workflow_run_or_status")

    data = response.get("data")
    if not isinstance(data, dict):
        return _passthrough(response, "workflow_response_without_data")

    if selected_tool == "skyvern_workflow_status":
        return _format_status_response(response, data)
    return _format_run_response(response, data)


__all__ = ["WorkflowToolName", "format_workflow_response"]
