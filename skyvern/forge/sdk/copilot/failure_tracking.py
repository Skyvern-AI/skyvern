"""Repeated-failure tracking for the copilot agent loop.

Computes two normalized keys per run:

- **failure signature**: "is this the same failure as last time?" —
  frontier_start_label + normalized failure reason + top failure category +
  suspicious-success flag.
- **frontier fingerprint**: SHA256 of the executed blocks' canonical config.
  Changes whenever the agent edits any block in the executed suffix.

A streak counter increments only when BOTH keys repeat. It resets on:
- a meaningful-data success
- a different frontier fingerprint
- a different failure signature

Enforcement uses the streak count to escalate nudges (see ``enforcement.py``).
This module does not itself decide when to stop the loop — it only maintains
the state enforcement reads.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from skyvern.forge.sdk.copilot.context import CopilotContext
    from skyvern.forge.sdk.workflow.models.workflow import WorkflowDefinition

_FAILURE_REASON_MAX_CHARS = 200


def _normalize_failure_reason(raw: str | None) -> str:
    if not raw:
        return ""
    collapsed = " ".join(raw.split())
    if len(collapsed) > _FAILURE_REASON_MAX_CHARS:
        collapsed = collapsed[:_FAILURE_REASON_MAX_CHARS]
    return collapsed.lower()


def _top_failure_category(failure_categories: list[dict] | None) -> str:
    if not failure_categories:
        return ""
    first = failure_categories[0]
    if isinstance(first, dict):
        return str(first.get("category") or "")
    return ""


def compute_failure_signature(
    frontier_start_label: str | None,
    failure_reason: str | None,
    failure_categories: list[dict] | None,
    suspicious_success: bool,
) -> str | None:
    """Return a normalized signature for the current failure, or ``None`` on success.

    ``None`` means "no signature — this was a real success". A suspicious-success
    run (status=completed but data-producing blocks produced no output) still
    generates a signature so repeated no-data runs can be counted as repeats.
    """
    normalized = _normalize_failure_reason(failure_reason)
    has_signal = bool(normalized) or suspicious_success
    if not has_signal:
        return None
    safe_label = frontier_start_label if isinstance(frontier_start_label, str) else ""
    top_category = _top_failure_category(failure_categories)
    # PARAMETER_BINDING_ERROR failure_reason embeds the offending key name, so
    # collapse it to a stable constant or repeats on different keys won't hash
    # to the same signature.
    if top_category == "PARAMETER_BINDING_ERROR":
        normalized = "parameter_binding_error"
    parts = [
        safe_label,
        normalized,
        top_category,
        "suspicious" if suspicious_success else "failed",
    ]
    return "|".join(parts)


def _canonical_block_config(block: Any) -> dict[str, Any]:
    """Stable dict view of a block's material config, with fields that don't
    affect downstream behavior (``output_parameter``) dropped.
    """
    dump = getattr(block, "model_dump", None)
    if callable(dump):
        try:
            cfg = dump(mode="json", exclude_none=True)
        except TypeError:
            cfg = dump()
    elif isinstance(block, dict):
        cfg = dict(block)
    else:
        return {"repr": repr(block)}
    cfg.pop("output_parameter", None)
    return cfg


def compute_action_sequence_fingerprint(results: list[dict[str, Any]]) -> str | None:
    """Hash the ordered ``(action_type, element_id)`` pairs across every
    block's ``action_trace`` in ``results``. Returns ``None`` when the trace is
    empty (e.g. fully-successful run where ``_attach_action_traces`` did not
    attach anything). Stable across runs: a form-fill→click→re-fill loop that
    retargets the same elements will produce the same fingerprint.
    """
    pairs: list[str] = []
    for entry in results:
        trace = entry.get("action_trace")
        if not isinstance(trace, list):
            continue
        for action in trace:
            if not isinstance(action, dict):
                continue
            action_type = action.get("action") or ""
            element = action.get("element") or ""
            pairs.append(f"{action_type}\x1f{element}")
    if not pairs:
        return None
    payload = "\x1e".join(pairs).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def compute_frontier_fingerprint(
    executed_labels: list[str],
    workflow_definition: WorkflowDefinition | None,
) -> str:
    """SHA256 of the executed blocks' canonical config, joined by label order.

    The fingerprint changes whenever a block's material config changes or the
    executed suffix itself changes. Returns an empty string when the workflow
    definition is missing — the caller treats "" as "can't fingerprint" and
    does not increment the streak on that run.
    """
    if not executed_labels or workflow_definition is None:
        return ""
    by_label: dict[str, Any] = {}
    blocks = getattr(workflow_definition, "blocks", None) or []
    for block in blocks:
        label = getattr(block, "label", None)
        if isinstance(label, str):
            by_label[label] = block
    payload: list[dict[str, Any]] = []
    for label in executed_labels:
        block = by_label.get(label)
        if block is None:
            payload.append({"label": label, "missing": True})
            continue
        payload.append({"label": label, "config": _canonical_block_config(block)})
    try:
        serialized = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    except (TypeError, ValueError):
        serialized = repr(payload)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _has_meaningful_success(result: dict[str, Any], suspicious_success: bool) -> bool:
    """Successful run AND the suffix produced meaningful data (not suspicious)."""
    return bool(result.get("ok")) and not suspicious_success


def update_repeated_failure_state(
    ctx: CopilotContext,
    result: dict[str, Any],
) -> None:
    """Update ``repeated_failure_streak_count`` and related fields on ``ctx``.

    Called after ``_record_run_blocks_result`` has populated the fail-mode
    fields (``last_test_suspicious_success``, ``last_test_anti_bot``,
    ``last_test_failure_reason``) and ``_run_blocks_and_collect_debug`` has
    set ``last_executed_block_labels`` and ``last_frontier_start_label``.
    """
    data = result.get("data") if isinstance(result, dict) else None
    failure_categories = None
    if isinstance(data, dict):
        raw_cats = data.get("failure_categories")
        if isinstance(raw_cats, list):
            failure_categories = raw_cats

    suspicious_success_raw = getattr(ctx, "last_test_suspicious_success", False)
    suspicious_success = bool(suspicious_success_raw) if isinstance(suspicious_success_raw, (bool, int)) else False
    failure_reason_raw = getattr(ctx, "last_test_failure_reason", None)
    failure_reason = failure_reason_raw if isinstance(failure_reason_raw, str) else None
    frontier_start_raw = getattr(ctx, "last_frontier_start_label", None)
    frontier_start_label = frontier_start_raw if isinstance(frontier_start_raw, str) else None
    executed_labels_raw = getattr(ctx, "last_executed_block_labels", None)
    executed_labels = (
        [label for label in executed_labels_raw if isinstance(label, str)]
        if isinstance(executed_labels_raw, list)
        else []
    )
    workflow_definition = None
    last_workflow = getattr(ctx, "last_workflow", None)
    if last_workflow is not None:
        candidate = getattr(last_workflow, "workflow_definition", None)
        if candidate is not None and hasattr(candidate, "blocks"):
            workflow_definition = candidate

    new_action_fingerprint_raw = getattr(ctx, "pending_action_sequence_fingerprint", None)
    new_action_fingerprint = new_action_fingerprint_raw if isinstance(new_action_fingerprint_raw, str) else None
    prior_action_fingerprint_raw = getattr(ctx, "last_action_sequence_fingerprint", None)
    prior_action_fingerprint = prior_action_fingerprint_raw if isinstance(prior_action_fingerprint_raw, str) else None

    if _has_meaningful_success(result, suspicious_success):
        ctx.last_failure_signature = None
        ctx.last_frontier_fingerprint = compute_frontier_fingerprint(executed_labels, workflow_definition)
        ctx.repeated_failure_streak_count = 0
        ctx.repeated_failure_nudge_emitted_at_streak = 0
        # Success resets the action-sequence streak. Promote the pending
        # fingerprint so the next failure run can compare against it.
        ctx.last_action_sequence_fingerprint = new_action_fingerprint
        ctx.pending_action_sequence_fingerprint = None
        ctx.repeated_action_fingerprint_streak_count = 0
        return

    signature = compute_failure_signature(
        frontier_start_label=frontier_start_label,
        failure_reason=failure_reason,
        failure_categories=failure_categories,
        suspicious_success=suspicious_success,
    )
    fingerprint = compute_frontier_fingerprint(executed_labels, workflow_definition)

    # Action-sequence streak runs independently of the frontier streak: a
    # repeated action sequence can fire even when the failure-reason text
    # changes turn to turn (e.g. different validation messages).
    if new_action_fingerprint is not None and new_action_fingerprint == prior_action_fingerprint:
        prior_action_streak_raw = getattr(ctx, "repeated_action_fingerprint_streak_count", 0)
        prior_action_streak = prior_action_streak_raw if isinstance(prior_action_streak_raw, int) else 0
        ctx.repeated_action_fingerprint_streak_count = prior_action_streak + 1
    elif new_action_fingerprint is not None:
        ctx.repeated_action_fingerprint_streak_count = 1
    else:
        # No action trace on this run (e.g. all blocks succeeded or no failed
        # blocks had a task_id). Don't reset — a transient empty trace between
        # two repeats shouldn't erase an in-progress streak.
        pass
    ctx.last_action_sequence_fingerprint = new_action_fingerprint
    ctx.pending_action_sequence_fingerprint = None

    if not signature or not fingerprint:
        ctx.last_failure_signature = signature
        ctx.last_frontier_fingerprint = fingerprint
        return

    prior_signature_raw = getattr(ctx, "last_failure_signature", None)
    prior_signature = prior_signature_raw if isinstance(prior_signature_raw, str) else None
    prior_fingerprint_raw = getattr(ctx, "last_frontier_fingerprint", None)
    prior_fingerprint = prior_fingerprint_raw if isinstance(prior_fingerprint_raw, str) else None
    if signature == prior_signature and fingerprint == prior_fingerprint:
        prior_streak_raw = getattr(ctx, "repeated_failure_streak_count", 0)
        prior_streak = prior_streak_raw if isinstance(prior_streak_raw, int) else 0
        ctx.repeated_failure_streak_count = prior_streak + 1
    else:
        ctx.repeated_failure_streak_count = 1
        # New frontier/signature restarts the nudge escalation cycle.
        ctx.repeated_failure_nudge_emitted_at_streak = 0

    ctx.last_failure_signature = signature
    ctx.last_frontier_fingerprint = fingerprint
