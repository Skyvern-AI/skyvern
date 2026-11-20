"""Turn-level outcome-verification telemetry for the workflow copilot.

The completion-verification verdict and the terminal-gate decision are produced
and consumed mid-turn, and ``ctx.completion_verification_result`` is cleared at
the start of every run-tool call. So the verdict and gate decision are captured
into a per-turn snapshot at the moment they are produced/consumed, and the
snapshot is attached to the copilot.turn span once at turn finalization — the
snapshot survives a later run-tool call that clears the live result field.
"""

from __future__ import annotations

from typing import Any

import structlog
from opentelemetry import trace as otel_trace

LOG = structlog.get_logger()

_SNAPSHOT_ATTR = "outcome_verification_trace_snapshot"
_COMPLETION_PREFIX = "completion_verification_"
_ABSTENTION_PREFIX = "plain_outcome_no_evidence_abstention_"
_ABSTENTION_LOG_EVENT = "copilot.completion.plain_outcome_no_evidence_abstention"
_CODE_ARTIFACT_VIOLATIONS_KEY = "copilot.code_artifact_violations"
_CODE_ARTIFACT_VIOLATION_LABELS_KEY = "copilot.code_artifact_violation_block_labels"
_CODE_ARTIFACT_VIOLATION_COUNT_KEY = "copilot.code_artifact_violation_count"


def _snapshot(ctx: Any) -> dict[str, Any] | None:
    snapshot = getattr(ctx, _SNAPSHOT_ATTR, None)
    if isinstance(snapshot, dict):
        return snapshot
    snapshot = {}
    try:
        setattr(ctx, _SNAPSHOT_ATTR, snapshot)
    except Exception:
        return None
    return snapshot


def record_gate_decision(ctx: Any, fields: dict[str, Any]) -> None:
    try:
        snapshot = _snapshot(ctx)
        if snapshot is not None:
            snapshot.update(fields)
    except Exception:
        LOG.warning("failed to record copilot gate decision telemetry", exc_info=True)


def record_criteria_lifecycle(ctx: Any, fields: dict[str, Any]) -> None:
    """Attach criteria-lifecycle fields (epoch, decision reason, tripwire counters,
    claim tier) to the per-turn snapshot so they ride the copilot.turn span."""
    try:
        snapshot = _snapshot(ctx)
        if snapshot is not None:
            snapshot.update(fields)
    except Exception:
        LOG.warning("failed to record copilot criteria lifecycle telemetry", exc_info=True)


def record_code_artifact_violations(ctx: Any, violations: list[str], offending_labels: list[str]) -> None:
    """Persist the code-artifact-metadata violation batch onto the turn snapshot.

    The strings carry schema field names and the graph's structural identifiers
    (block labels, claim/dependency/expectation ids) but never free-text content,
    so they ride the prod span verbatim without value-scrubbing.
    """
    try:
        if not violations:
            return
        snapshot = _snapshot(ctx)
        if snapshot is None:
            return
        snapshot[_CODE_ARTIFACT_VIOLATIONS_KEY] = [str(message) for message in violations]
        snapshot[_CODE_ARTIFACT_VIOLATION_LABELS_KEY] = sorted({str(label) for label in offending_labels if str(label)})
        snapshot[_CODE_ARTIFACT_VIOLATION_COUNT_KEY] = len(violations)
    except Exception:
        LOG.warning("failed to record copilot code artifact violation telemetry", exc_info=True)


def record_completion_verification(ctx: Any, result: Any | None, workflow_run_id: str | None = None) -> None:
    """Refresh the snapshot's completion-verification block for the latest run.

    Called on every recorded run, evaluated or not, so the snapshot describes the
    turn's most recent run and never preserves a stale verdict from an earlier one.
    """
    try:
        trace_data: dict[str, Any] | None = None
        if result is not None and getattr(result, "status", None) == "evaluated":
            trace_data = result.to_trace_data()
            # Span attributes are refreshed by later runs in the same turn, so the
            # abstention fingerprint is also emitted as a durable log line.
            if trace_data.get(f"{_ABSTENTION_PREFIX}engaged") is True:
                LOG.info(
                    _ABSTENTION_LOG_EVENT,
                    workflow_run_id=workflow_run_id,
                    fully_satisfied=trace_data.get("fully_satisfied"),
                    **{key: value for key, value in trace_data.items() if key.startswith(_ABSTENTION_PREFIX)},
                )
        snapshot = _snapshot(ctx)
        if snapshot is None:
            return
        for key in [key for key in snapshot if key.startswith(_COMPLETION_PREFIX)]:
            del snapshot[key]
        if trace_data is not None:
            snapshot.update({f"{_COMPLETION_PREFIX}{key}": value for key, value in trace_data.items()})
            snapshot[f"{_COMPLETION_PREFIX}evaluated_on_final_run"] = True
        else:
            snapshot[f"{_COMPLETION_PREFIX}status"] = getattr(result, "status", None) or "not_run"
            snapshot[f"{_COMPLETION_PREFIX}evaluated_on_final_run"] = False
    except Exception:
        LOG.warning("failed to record copilot completion verification telemetry", exc_info=True)


def outcome_verification_turn_fields(ctx: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    snapshot = getattr(ctx, _SNAPSHOT_ATTR, None)
    if isinstance(snapshot, dict):
        fields.update(snapshot)
    evidence = getattr(ctx, "workflow_verification_evidence", None)
    if evidence is not None and hasattr(evidence, "to_trace_data"):
        fields.update({f"verification_evidence_{key}": value for key, value in evidence.to_trace_data().items()})
    policy = getattr(ctx, "request_policy", None)
    if policy is not None and hasattr(policy, "to_trace_data"):
        fields.update({f"request_policy_{key}": value for key, value in policy.to_trace_data().items()})
    return _otel_safe(fields)


def finalize_outcome_verification_trace(ctx: Any, span: Any = None) -> None:
    """Attach the accumulated snapshot to the copilot.turn span once per turn.

    Strictly best-effort: a telemetry failure here must never convert a valid
    turn into an unexpected-error result.
    """
    try:
        if ctx is None:
            return
        fields = outcome_verification_turn_fields(ctx)
        if not fields:
            return
        target = span if span is not None else otel_trace.get_current_span()
        target.set_attributes(fields)
    except Exception:
        LOG.warning("failed to finalize copilot outcome verification telemetry", exc_info=True)


def _otel_safe(fields: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in fields.items():
        if isinstance(value, (bool, int, float, str)):
            safe[key] = value
        elif isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value):
            safe[key] = list(value)
        elif value is None:
            continue
        else:
            safe[key] = str(value)
    return safe
