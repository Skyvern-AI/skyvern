"""Validation guard for undeclared `context.parameters[X]` references.

Catches the phantom-parameter pattern at the end of initial script generation
(SKY-8965). Complements `ScriptReviewer._validate_parameter_references`, which
runs only during the reviewer retry path — this guard runs at the initial-gen
pipeline stage, where the existing reviewer validator never sees the code.

Phase 1 (this PR): guard runs in WARNING mode. It logs violations with full
context but does not raise. This gives us a production signal in Datadog for
hallucination frequency before we decide to escalate.

Phase 2 (follow-up): flip the guard to raise on violation. At that point
workflow authors either declare their parameters up front, or the script
generator falls back to `ai='proactive'` for the undeclared fields (Phase 2
change in `generate_workflow_parameters.py`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import structlog

LOG = structlog.get_logger(__name__)


# Matches both subscript (`context.parameters['key']`) and dict-get
# (`context.parameters.get('key')` or `.get('key', default)`) access. The
# current initial-gen emits subscripts only, but the reviewer retry path and
# its prompt templates use `.get()`, so the guard handles both to stay
# consistent with any future unification with ScriptReviewer._PARAM_REF_RE.
# Attribute access (`context.parameters.key`) is NOT emitted anywhere today,
# so it's intentionally excluded — add it if Phase 2 introduces it.
_PARAM_REF_RE = re.compile(r"""context\.parameters(?:\[['"](\w+)['"]\]|\.get\(\s*['"](\w+)['"]\s*(?:,[^)]*)?\))""")


@dataclass(frozen=True)
class UndeclaredReference:
    key: str
    # One example location for debugging; we don't collect every occurrence.
    example_line: str


@dataclass
class GuardResult:
    """Result of validating a generated script against the valid-keys set."""

    valid: bool
    undeclared_refs: list[UndeclaredReference] = field(default_factory=list)
    referenced_keys: frozenset[str] = frozenset()
    valid_keys: frozenset[str] = frozenset()

    def format_error(self) -> str:
        """Human-readable error string for logs / raised exceptions."""
        invalid_list = ", ".join(repr(r.key) for r in self.undeclared_refs)
        valid_list = ", ".join(repr(k) for k in sorted(self.valid_keys)) or "(none)"
        return (
            f"Generated script references undeclared workflow parameters: {invalid_list}. "
            f"Valid keys are: {valid_list}. This usually means the synthesis LLM invented a "
            f"field name for a value that was a literal in the navigation goal. See SKY-8965."
        )


def _collect_refs(code: str) -> list[UndeclaredReference]:
    """Walk the code line-by-line and collect every context.parameters[...] reference.

    Comments are skipped. Multiple references per line produce multiple entries.
    """
    refs: list[UndeclaredReference] = []
    for line in code.split("\n"):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        for match in _PARAM_REF_RE.finditer(line):
            # Group 1 = subscript form, group 2 = .get() form; exactly one fires.
            key = match.group(1) or match.group(2)
            if key:
                refs.append(UndeclaredReference(key=key, example_line=line.strip()))
    return refs


def validate_context_parameter_refs(
    *,
    code: str,
    declared_param_keys: frozenset[str],
    upstream_schema_keys: frozenset[str],
    synthesized_keys: frozenset[str] = frozenset(),
) -> GuardResult:
    """Validate that every `context.parameters[X]` in `code` references a known key.

    The valid-keys set is the union of:
      - `declared_param_keys`: every key declared in
        `workflow_definition.parameters` regardless of parameter_type (workflow,
        output, context, secret — all are legal `context.parameters[X]` targets
        at runtime).
      - `upstream_schema_keys`: keys exposed by upstream extract-info / task
        blocks via their `data_schema.properties` (recursed into ForLoop
        children).
      - `synthesized_keys`: keys present in the generated `GeneratedWorkflowParameters`
        Pydantic class. Callable from Phase 1 to stay compatible with the current
        LLM synthesis path; Phase 2 will shrink this to empty and rely entirely
        on the first two sets.

    Args:
        code: The Python source of the generated script (usually `main.py` +
            concatenated block `.skyvern` files). Pass as a single string.
        declared_param_keys: Frozenset of declared workflow parameter keys.
        upstream_schema_keys: Frozenset of keys exposed by upstream blocks.
        synthesized_keys: Frozenset of field names currently present in the
            synthesized `GeneratedWorkflowParameters` class.

    Returns:
        A `GuardResult` with `valid=False` when any reference falls outside the
        valid set. The caller decides whether to log (Phase 1) or raise
        (Phase 2).
    """
    refs = _collect_refs(code)
    referenced_keys = frozenset(r.key for r in refs)
    valid_keys = declared_param_keys | upstream_schema_keys | synthesized_keys

    undeclared = [r for r in refs if r.key not in valid_keys]
    return GuardResult(
        valid=not undeclared,
        undeclared_refs=undeclared,
        referenced_keys=referenced_keys,
        valid_keys=valid_keys,
    )


class HallucinatedParameterError(RuntimeError):
    """Raised in Phase 2 when the guard rejects the generated script.

    Phase 1 does not raise; it only logs. The exception class is defined now
    so downstream code can be written against it ahead of the flip.
    """

    def __init__(self, result: GuardResult) -> None:
        super().__init__(result.format_error())
        self.result = result


def log_or_raise_guard_result(
    result: GuardResult,
    *,
    raise_on_violation: bool,
    workflow_permanent_id: str | None = None,
    workflow_run_id: str | None = None,
) -> None:
    """Apply the Phase 1 / Phase 2 policy to a guard result.

    Phase 1: `raise_on_violation=False`, we only log a warning.
    Phase 2: `raise_on_violation=True`, we also raise `HallucinatedParameterError`.
    """
    if result.valid:
        return

    LOG.warning(
        "parameter_reference_guard_violation",
        workflow_permanent_id=workflow_permanent_id,
        workflow_run_id=workflow_run_id,
        undeclared_refs=sorted(r.key for r in result.undeclared_refs),
        valid_keys=sorted(result.valid_keys),
        sky_ticket="SKY-8965",
    )
    if raise_on_violation:
        raise HallucinatedParameterError(result)
