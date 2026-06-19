"""Repeated-failure tracking for the copilot agent loop.

Computes two normalized keys per run:

- **failure signature**: "is this the same failure as last time?" —
  structural root-cause identity when available, otherwise normalized failure
  reason + top failure category + suspicious-success flag.
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
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from skyvern.forge.sdk.copilot.completion_verification import CompletionVerificationResult
    from skyvern.forge.sdk.copilot.context import CopilotContext
    from skyvern.forge.sdk.workflow.models.workflow import WorkflowDefinition

_FAILURE_REASON_MAX_CHARS = 200
_ROOT_CAUSE_SIGNATURE_VERSION = "repair_root_cause:v1"

# Stable identifier for the per-tool budget failure category written into
# ``failure_categories`` by the watchdog. Used by enforcement, reconciliation,
# and signature normalization as a single source of truth.
PER_TOOL_BUDGET_FAILURE_CATEGORY = "PER_TOOL_BUDGET"
ANTI_BOT_CHALLENGE_ROOT_CAUSE_CATEGORY = "ANTI_BOT_CHALLENGE"

# Stable active-run terminal evidence identifiers. The category is stored in
# tool result ``failure_categories``; the reason code is stored on blocker
# signals that convert that tool result into product-safe final copy.
ACTIVE_RUN_TERMINAL_EVIDENCE_FAILURE_CATEGORY = "ACTIVE_RUN_TERMINAL_EVIDENCE"
ACTIVE_RUN_TERMINAL_EVIDENCE_REASON_CODE = "tool_error_active_run_terminal_evidence"

_ROOT_CAUSE_CATEGORY_ALIASES = {
    "ANTI_BOT_DETECTION": ANTI_BOT_CHALLENGE_ROOT_CAUSE_CATEGORY,
    "CHALLENGE_DETECTION": ANTI_BOT_CHALLENGE_ROOT_CAUSE_CATEGORY,
    "HUMAN_VERIFICATION_CHALLENGE": ANTI_BOT_CHALLENGE_ROOT_CAUSE_CATEGORY,
    # Self-mappings document categories whose noisy failure prose must not
    # affect repeat identity.
    "PARAMETER_BINDING_ERROR": "PARAMETER_BINDING_ERROR",
    PER_TOOL_BUDGET_FAILURE_CATEGORY: PER_TOOL_BUDGET_FAILURE_CATEGORY,
}
ANTI_BOT_CHALLENGE_FAILURE_CATEGORIES = frozenset(
    {
        ANTI_BOT_CHALLENGE_ROOT_CAUSE_CATEGORY,
        *(
            category
            for category, normalized in _ROOT_CAUSE_CATEGORY_ALIASES.items()
            if normalized == ANTI_BOT_CHALLENGE_ROOT_CAUSE_CATEGORY
        ),
    }
)
_CATEGORY_ONLY_ROOT_CAUSES = frozenset(
    {
        ANTI_BOT_CHALLENGE_ROOT_CAUSE_CATEGORY,
        "PARAMETER_BINDING_ERROR",
        PER_TOOL_BUDGET_FAILURE_CATEGORY,
    }
)
_LOCATOR_RE = re.compile(r"""(?:page\.)?locator\(\s*(["'])(?P<selector>.+?)\1""")
_SELECTOR_QUOTED_RE = re.compile(
    r"""(?:selector|css selector|target selector|resolved selector)\s*[:=]\s*(["'])(?P<selector>.+?)\1""",
    re.IGNORECASE,
)
_SELECTOR_UNQUOTED_RE = re.compile(
    r"""(?:selector|css selector|target selector|resolved selector)\s*[:=]\s*(?P<selector>[#.\[\]:=\w\-/]+)""",
    re.IGNORECASE,
)
_ROLE_RE = re.compile(
    r"""get_by_role\(\s*(["'])(?P<role>.+?)\1(?:\s*,\s*name\s*=\s*(["'])(?P<name>.*?)\3)?""",
    re.IGNORECASE,
)
_EXCEPTION_CLASS_RE = re.compile(r"""\b(?P<class>[A-Za-z_][\w.]*?(?:Error|Exception))\s*:""")
# ``browser_context.mode=none`` is the stable enum-backed value surfaced by
# local browser-state failures, unlike surrounding natural-language prose.
_BROWSER_SESSION_ERROR_RE = re.compile(
    r"\b(browser session not found|no browser context|no active browser|browser_context\.mode=none|"
    r"no_active_browser|browser_not_found)\b",
    re.IGNORECASE,
)
_TIMEOUT_ERROR_RE = re.compile(r"\b(timeout(?:error)?|timed out)\b", re.IGNORECASE)
_CODE_BLOCK_IMPOSITION_DROPPED_RE = re.compile(
    r"unable to impose synthesized code block:\s*dropped scout interaction\s+\d+\s+"
    r"from\s+`?(?P<tool>[A-Za-z0-9_]+)`?\s+\((?P<reason>[A-Za-z0-9_]+)\)",
    re.IGNORECASE,
)
_CODE_BLOCK_IMPOSITION_RE = re.compile(
    r"unable to impose synthesized code block:\s*(?P<reason>[^\n.]+)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RepairRootCauseIdentity:
    root_cause_signature: str | None = None
    primary_category: str = ""
    failure_categories: tuple[str, ...] = ()
    error_class: str = ""
    selector_kind: str = ""
    selector: str = ""


def normalize_failure_reason(raw: str | None) -> str:
    if not raw:
        return ""
    collapsed = " ".join(raw.split())
    if len(collapsed) > _FAILURE_REASON_MAX_CHARS:
        collapsed = collapsed[:_FAILURE_REASON_MAX_CHARS]
    return collapsed.lower()


def _category_name(value: Mapping[str, Any] | str) -> str:
    raw = value.get("category") if isinstance(value, Mapping) else value
    return str(raw or "").strip().upper()


def _normalized_root_cause_categories(
    failure_categories: Sequence[Mapping[str, Any] | str] | None,
    detected_challenge: bool,
) -> tuple[str, ...]:
    categories: set[str] = set()
    for entry in failure_categories or ():
        category = _category_name(entry)
        if not category:
            continue
        categories.add(_ROOT_CAUSE_CATEGORY_ALIASES.get(category, category))
    if detected_challenge:
        categories.add(ANTI_BOT_CHALLENGE_ROOT_CAUSE_CATEGORY)
    return tuple(sorted(categories))


def _normalize_signature_text(value: str) -> str:
    return " ".join(value.strip().split()).rstrip(".,;")


def _selector_from_text(text: str) -> tuple[str, str]:
    for pattern, kind in (
        (_LOCATOR_RE, "locator"),
        (_SELECTOR_QUOTED_RE, "selector"),
        (_SELECTOR_UNQUOTED_RE, "selector"),
    ):
        match = pattern.search(text)
        if match:
            return kind, _normalize_signature_text(match.group("selector"))
    role_match = _ROLE_RE.search(text)
    if role_match:
        role = _normalize_signature_text(role_match.group("role"))
        name = _normalize_signature_text(role_match.group("name") or "")
        return "role", f"{role}:{name}" if name else role
    return "", ""


def _error_class_from_text(text: str) -> str:
    code_block_imposition = _code_block_imposition_error_class(text)
    if code_block_imposition:
        return code_block_imposition
    if _BROWSER_SESSION_ERROR_RE.search(text):
        return "browser_session_not_found"
    match = _EXCEPTION_CLASS_RE.search(text)
    if match:
        return re.sub(r"(?<!^)(?=[A-Z])", "_", match.group("class").split(".")[-1]).lower()
    if _TIMEOUT_ERROR_RE.search(text):
        return "timeout_error"
    return ""


def _code_block_imposition_error_class(text: str) -> str:
    dropped_match = _CODE_BLOCK_IMPOSITION_DROPPED_RE.search(text)
    if dropped_match:
        return f"code_block_synthesis_{_snake_token(dropped_match.group('reason'))}"
    match = _CODE_BLOCK_IMPOSITION_RE.search(text)
    if match:
        return f"code_block_synthesis_{_snake_token(match.group('reason'))}"
    return ""


def _snake_token(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9]+", "_", value.strip().lower()).strip("_")
    return normalized or "unknown"


def _root_cause_texts(
    failure_reason: str | None,
    error_texts: Sequence[str | None] | None,
    blocks: Sequence[Mapping[str, Any]] | None,
) -> list[str]:
    texts = [text for text in ([failure_reason] + list(error_texts or ())) if isinstance(text, str) and text.strip()]
    for block in blocks or ():
        for key in ("failure_reason", "error", "message"):
            value = block.get(key)
            if isinstance(value, str) and value.strip():
                texts.append(value)
    return texts


def compute_repair_root_cause_signature(
    *,
    failure_categories: Sequence[Mapping[str, Any] | str] | None = None,
    failure_reason: str | None = None,
    error_texts: Sequence[str | None] | None = None,
    blocks: Sequence[Mapping[str, Any]] | None = None,
    detected_challenge: bool = False,
) -> RepairRootCauseIdentity:
    categories = _normalized_root_cause_categories(failure_categories, detected_challenge)
    primary_category = categories[0] if categories else ""
    texts = _root_cause_texts(failure_reason, error_texts, blocks)
    error_class = ""
    selector_kind = ""
    selector = ""
    for text in texts:
        if not error_class:
            error_class = _error_class_from_text(text)
        if not selector:
            selector_kind, selector = _selector_from_text(text)
        if error_class and selector:
            break

    if set(categories) & _CATEGORY_ONLY_ROOT_CAUSES:
        error_class = ""
        selector_kind = ""
        selector = ""
    if not categories and not error_class and not selector:
        return RepairRootCauseIdentity()

    payload = {
        "version": _ROOT_CAUSE_SIGNATURE_VERSION,
        "failure_categories": categories,
        "error_class": error_class,
        "selector_kind": selector_kind,
        "selector": selector,
    }
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    signature = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    return RepairRootCauseIdentity(
        root_cause_signature=signature,
        primary_category=primary_category,
        failure_categories=categories,
        error_class=error_class,
        selector_kind=selector_kind,
        selector=selector,
    )


def _top_failure_category(failure_categories: list[dict] | None) -> str:
    if not failure_categories:
        return ""
    first = failure_categories[0]
    if isinstance(first, dict):
        return str(first.get("category") or "")
    return ""


def _failure_identity_terms(
    failure_reason: str | None,
    failure_categories: list[dict] | None,
    suspicious_success: bool,
    detected_challenge: bool = False,
) -> list[str] | None:
    """Category-stable failure half shared by both signatures, or ``None`` on a
    real success. Per-call-data categories collapse to a constant so consecutive
    trips hash identically."""
    normalized = normalize_failure_reason(failure_reason)
    has_signal = bool(normalized) or bool(failure_categories) or suspicious_success or detected_challenge
    if not has_signal:
        return None
    structural_identity = compute_repair_root_cause_signature(
        failure_categories=failure_categories,
        failure_reason=failure_reason,
        detected_challenge=detected_challenge,
    )
    terminal_state = "suspicious" if suspicious_success else "failed"
    if structural_identity.root_cause_signature:
        return [structural_identity.root_cause_signature]
    top_category = _top_failure_category(failure_categories)
    if top_category == "PARAMETER_BINDING_ERROR":
        normalized = "parameter_binding_error"
    elif top_category == PER_TOOL_BUDGET_FAILURE_CATEGORY:
        normalized = "per_tool_budget"
    return [normalized, top_category, terminal_state]


def compute_failure_signature(
    frontier_start_label: str | None,
    failure_reason: str | None,
    failure_categories: list[dict] | None,
    suspicious_success: bool,
    detected_challenge: bool = False,
) -> str | None:
    """Return a normalized signature for the current failure, or ``None`` on success.

    ``None`` means "no signature — this was a real success". A suspicious-success
    run (status=completed but data-producing blocks produced no output) still
    generates a signature so repeated no-data runs can be counted as repeats.

    ``frontier_start_label`` is kept for call-site compatibility. Labels are
    intentionally excluded from the signature because block renames are not a
    new root cause.
    """
    terms = _failure_identity_terms(failure_reason, failure_categories, suspicious_success, detected_challenge)
    if terms is None:
        return None
    return "|".join(terms)


def satisfied_criterion_ids(result: CompletionVerificationResult | None) -> frozenset[str]:
    """Criterion ids the outcome-verification judge confirmed satisfied (evidence_confirms),
    or empty when no judge result is available for this run."""
    if result is None or result.status != "evaluated":
        return frozenset()
    return frozenset(verdict.criterion_id for verdict in result.verdicts if verdict.reason_code == "evidence_confirms")


def made_newly_verified_progress(
    current_satisfied: frozenset[str],
    high_water: frozenset[str],
    full_workflow_verified_this_run: bool,
    verified_prefix_grew: bool,
) -> bool:
    """Whether this run advanced verified progress past the turn high-water: a newly
    confirmed criterion, a clean end-to-end run, or a grown verified block prefix."""
    return bool(current_satisfied - high_water) or full_workflow_verified_this_run or verified_prefix_grew


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
    detected_challenge = bool(getattr(ctx, "last_test_anti_bot", None))
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
        detected_challenge=detected_challenge,
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
