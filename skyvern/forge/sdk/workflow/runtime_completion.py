"""Runtime grading of a workflow's declared completion contract.

A workflow version can carry a small typed statement of what a run must produce. Grading it at
finalization — after files register, before the status write — is what lets a run that produced
nothing report that, no matter which execution engine ran the blocks or how the block's own code
chose to describe its outcome.

Contract-less workflows are untouched: the grader has nothing to say about a run whose workflow
never declared an outcome.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import structlog

LOG = structlog.get_logger()

# Each kind is graded only from the execution layer's own registration of that kind of file, which
# no generated code can fake or swallow: a site download, or a file the secure worker published.
CRITERION_REGISTERED_DOWNLOAD = "registered_download"
CRITERION_GENERATED_FILE = "generated_file"

_CONTRACT_KEY = "completion_contract"
_SUPPORTED_KINDS = frozenset({CRITERION_REGISTERED_DOWNLOAD, CRITERION_GENERATED_FILE})
_UNMET_REASONS = {
    CRITERION_REGISTERED_DOWNLOAD: "The workflow did not produce the file it is declared to download.",
    CRITERION_GENERATED_FILE: "The workflow did not produce the file it is declared to generate.",
}
# The field is public on the workflow schema, so a hand-written contract is bounded here.
_MAX_CRITERIA = 16
_MAX_ID_CHARS = 128


@dataclass(frozen=True)
class CompletionCriterion:
    id: str
    kind: str
    min_count: int = 1


@dataclass(frozen=True)
class ContractVerdict:
    satisfied: bool
    unmet_criterion_ids: tuple[str, ...]
    reason: str | None


def parse_completion_contract(workflow_definition: object) -> tuple[CompletionCriterion, ...]:
    """Read the typed criteria a workflow version declares, ignoring anything unrecognized.

    Unknown kinds are dropped rather than failing the parse: an older worker must keep running a
    workflow authored by a newer one, and a criterion it cannot grade is not a criterion it may
    treat as unmet."""
    raw = (
        workflow_definition.get(_CONTRACT_KEY)
        if isinstance(workflow_definition, dict)
        else getattr(workflow_definition, _CONTRACT_KEY, None)
    )
    if not isinstance(raw, dict):
        return ()
    criteria = raw.get("criteria")
    if not isinstance(criteria, list):
        return ()
    parsed: list[CompletionCriterion] = []
    for item in criteria[:_MAX_CRITERIA]:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").strip()
        if kind not in _SUPPORTED_KINDS:
            continue
        identifier = (str(item.get("id") or kind).strip() or kind)[:_MAX_ID_CHARS]
        try:
            min_count = int(item.get("min_count", 1))
        except (TypeError, ValueError):
            min_count = 1
        parsed.append(CompletionCriterion(id=identifier, kind=kind, min_count=max(1, min_count)))
    return tuple(parsed)


def grade_completion_contract(
    criteria: tuple[CompletionCriterion, ...],
    *,
    registered_download_count: int,
    generated_file_count: int = 0,
) -> ContractVerdict:
    """Grade declared criteria against execution-layer evidence."""
    unmet: list[CompletionCriterion] = []
    unclaimed_generated = generated_file_count
    for criterion in criteria:
        if criterion.kind == CRITERION_REGISTERED_DOWNLOAD:
            if registered_download_count < criterion.min_count:
                unmet.append(criterion)
        elif criterion.kind == CRITERION_GENERATED_FILE:
            # A published file is not tied to a criterion, so each promise claims its own in declaration
            # order: the unmet ids say how many promises are short, not which files are missing.
            if unclaimed_generated < criterion.min_count:
                unmet.append(criterion)
            else:
                unclaimed_generated -= criterion.min_count
    if not unmet:
        return ContractVerdict(satisfied=True, unmet_criterion_ids=(), reason=None)
    return ContractVerdict(
        satisfied=False,
        unmet_criterion_ids=tuple(criterion.id for criterion in unmet),
        reason=_UNMET_REASONS[unmet[0].kind],
    )


def contract_from_code_artifact_metadata(metadata: object) -> dict[str, object] | None:
    """Project the acting model's structured deliverable declaration into the runtime contract.

    Interactive request classification is retired. Code-artifact metadata is the model-owned,
    machine-generated statement of what an authored artifact promises; the server validates the
    closed enum and never infers that promise from prose or code shape.
    """
    if not isinstance(metadata, Mapping):
        return None
    # Ids are model-chosen per block, so two blocks can reuse one id for promises of different kinds.
    seen: set[tuple[str, str]] = set()
    criteria: list[dict[str, object]] = []
    for artifact in metadata.values():
        if not isinstance(artifact, Mapping):
            continue
        raw_criteria = artifact.get("completion_criteria")
        if not isinstance(raw_criteria, list):
            continue
        for item in raw_criteria:
            kind = item.get("deliverable_kind") if isinstance(item, Mapping) else None
            if not isinstance(kind, str) or kind not in _SUPPORTED_KINDS:
                continue
            identifier = (str(item.get("id") or kind).strip() or kind)[:_MAX_ID_CHARS]
            if (identifier, kind) in seen:
                continue
            seen.add((identifier, kind))
            criteria.append({"id": identifier, "kind": kind, "min_count": 1})
    if not criteria:
        return None
    return {"schema_version": 1, "criteria": criteria}


def carried_contract(existing_definition: object) -> dict[str, object] | None:
    """The contract already stored on a workflow version, if any."""
    raw = (
        existing_definition.get(_CONTRACT_KEY)
        if isinstance(existing_definition, dict)
        else getattr(existing_definition, _CONTRACT_KEY, None)
    )
    return raw if isinstance(raw, dict) else None


def with_contract(definition: dict, carried: dict[str, object] | None) -> dict:
    """Preserve a stored contract across a write that did not carry one.

    Every non-copilot save path rebuilds the definition through models that do not know this field,
    so without this a builder edit anywhere in the workflow would silently drop the obligation."""
    if carried is None or definition.get(_CONTRACT_KEY) is not None:
        return definition
    definition[_CONTRACT_KEY] = carried
    return definition
