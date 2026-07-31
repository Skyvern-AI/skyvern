"""Runtime grading of a workflow's declared completion contract.

A workflow version can carry a small typed statement of what a run must produce. Grading it at
finalization — after files register, before the status write — is what lets a run that produced
nothing report that, no matter which execution engine ran the blocks or how the block's own code
chose to describe its outcome.

Contract-less workflows are untouched: the grader has nothing to say about a run whose workflow
never declared an outcome.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

LOG = structlog.get_logger()

# The only criterion kind graded today. Its evidence is the execution layer's own file registration,
# which no generated code can fake or swallow.
CRITERION_REGISTERED_DOWNLOAD = "registered_download"

_CONTRACT_KEY = "completion_contract"
_SUPPORTED_KINDS = frozenset({CRITERION_REGISTERED_DOWNLOAD})
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
) -> ContractVerdict:
    """Grade declared criteria against execution-layer evidence."""
    unmet: list[str] = []
    for criterion in criteria:
        if criterion.kind == CRITERION_REGISTERED_DOWNLOAD and registered_download_count < criterion.min_count:
            unmet.append(criterion.id)
    if not unmet:
        return ContractVerdict(satisfied=True, unmet_criterion_ids=(), reason=None)
    return ContractVerdict(
        satisfied=False,
        unmet_criterion_ids=tuple(unmet),
        reason="The workflow did not produce the file it is declared to download.",
    )


def contract_from_request_criteria(criteria: object) -> dict[str, object] | None:
    """Project the request's own typed completion criteria into a workflow-carried contract.

    The obligation comes from what the user asked for, never from the shape of the code the product
    generated: a block that merely reports a download-shaped result promises nothing, and a request
    that asked for a file promises one however the block is written."""
    if not isinstance(criteria, (list, tuple)):
        return None
    if not any(_criterion_requests_a_download(criterion) for criterion in criteria):
        return None
    return {
        "schema_version": 1,
        "criteria": [{"id": "requested_download", "kind": CRITERION_REGISTERED_DOWNLOAD, "min_count": 1}],
    }


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


def _criterion_requests_a_download(criterion: object) -> bool:
    """Whether a request criterion asks for a registered download.

    The request classifier types this as ``deliverable_kind``/``output_path``; the synthetic id is
    the copilot's own internal marker for the same obligation. Both are request-derived."""
    from skyvern.forge.sdk.copilot.reached_download_target import REGISTERED_DOWNLOAD_REQUESTED_OUTPUT_PATHS
    from skyvern.forge.sdk.copilot.request_policy import REGISTERED_DOWNLOAD_COMPLETION_CRITERION_ID

    for attr in ("deliverable_kind", "declared_deliverable_kind"):
        if str(getattr(criterion, attr, "") or "").strip() == CRITERION_REGISTERED_DOWNLOAD:
            return True
    if str(getattr(criterion, "output_path", "") or "").strip() in REGISTERED_DOWNLOAD_REQUESTED_OUTPUT_PATHS:
        return True
    # The copilot's own synthetic marker for the same obligation, compared by id so this stays
    # readable from an untyped criterion.
    return str(getattr(criterion, "id", "") or "") == REGISTERED_DOWNLOAD_COMPLETION_CRITERION_ID
