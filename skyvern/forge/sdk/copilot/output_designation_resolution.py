"""Resolve which observed page value implements a requested output, when a packet first offers one.

Every other route to a designation keys on the model reaching an authoring step, calling a
particular tool, or asking a question — all of which can happen before the requested value has
rendered, and none of which re-fire once it has (SKY-13485). This keys on the page evidence itself,
the one event that cannot arrive early.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

import structlog
from typing_extensions import TypedDict

from skyvern.forge.sdk.copilot.output_extraction_plan import candidate_page_context, unbound_candidate_relations

LOG = structlog.get_logger(__name__)

RESOLUTION_PROMPT_TEMPLATE = "workflow-copilot-output-designation"
_MAX_CANDIDATES = 8


@dataclass(frozen=True)
class DesignationCandidate:
    label: str
    value_text: str


@dataclass(frozen=True)
class DesignationOpportunity:
    """A packet offering candidate values for requested outputs nothing has bound yet."""

    unbound_paths: tuple[str, ...]
    candidates: tuple[DesignationCandidate, ...]
    fingerprint: str
    page_context: str = ""


def _structural_fingerprint(
    unbound_paths: tuple[str, ...], candidates: tuple[DesignationCandidate, ...], page_context: str
) -> str:
    """Identity of the decision, not of the moment.

    Keyed on the requested paths, the candidate *labels*, and the page they were read from — never
    their values, so a metric ticking 1.42K -> 1.43K re-renders the same page without re-arming the
    resolver. The page belongs in the identity because it is an input to the decision: the same
    generic labels on an unfiltered and a filtered page are two different questions.
    """
    payload = json.dumps(
        {
            "paths": sorted(unbound_paths),
            "labels": sorted(candidate.label for candidate in candidates),
            "page": page_context,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def designation_opportunity(
    *,
    unbound_paths: set[str],
    flow_evidence: list[dict[str, Any]],
    resolved_fingerprints: set[str],
) -> DesignationOpportunity | None:
    """The decision this packet newly offers, or None when there is nothing to decide."""
    if not unbound_paths:
        return None
    offered = unbound_candidate_relations(flow_evidence, limit=_MAX_CANDIDATES)
    if not offered:
        return None
    candidates = tuple(DesignationCandidate(label=label, value_text=value) for label, value in offered)
    paths = tuple(sorted(unbound_paths))
    # The page's own query/filter is what makes a generic label ("logs found") specific to what was
    # asked for, so the decision needs it as much as it needs the candidate list.
    page_context = candidate_page_context(flow_evidence)
    fingerprint = _structural_fingerprint(paths, candidates, page_context)
    if fingerprint in resolved_fingerprints:
        return None
    return DesignationOpportunity(
        unbound_paths=paths,
        candidates=candidates,
        fingerprint=fingerprint,
        page_context=page_context,
    )


def render_candidates(opportunity: DesignationOpportunity) -> str:
    return "\n".join(
        f"{index}. {candidate.label} = {candidate.value_text}" for index, candidate in enumerate(opportunity.candidates)
    )


def render_requested_paths(opportunity: DesignationOpportunity, labels_by_path: dict[str, tuple[str, ...]]) -> str:
    """Each path with what the request said it means — the typed criterion outcome, not a goal blob."""
    lines = []
    for path in opportunity.unbound_paths:
        outcomes = " / ".join(label for label in labels_by_path.get(path, ()) if label)
        lines.append(f"- {path}: {outcomes}" if outcomes else f"- {path}")
    return "\n".join(lines)


class RequestedOutputRead(TypedDict, total=False):
    """A value read off the page for a requested output, for the page to pin to its element."""

    output_path: str
    value_text: str
    label: str


def coerce_resolution(raw: object, opportunity: DesignationOpportunity) -> list[RequestedOutputRead]:
    """Selections the page can be asked to pin, dropping anything the packet did not offer.

    A hallucinated index or path yields no read rather than a guess, which keeps the abstain path
    ("none") and a malformed answer structurally identical.
    """
    payload = raw
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            return []
    if not isinstance(payload, dict):
        return []
    selections = payload.get("selections")
    if not isinstance(selections, list):
        return []
    allowed = set(opportunity.unbound_paths)
    reads: list[RequestedOutputRead] = []
    claimed_paths: set[str] = set()
    claimed_candidates: set[int] = set()
    for selection in selections:
        if not isinstance(selection, dict):
            continue
        path = str(selection.get("output_path") or "")
        if path not in allowed or path in claimed_paths:
            continue
        index = selection.get("candidate_index")
        # bool is an int in Python, so `true` would otherwise select candidate 1.
        if not isinstance(index, int) or isinstance(index, bool):
            continue
        if not 0 <= index < len(opportunity.candidates):
            continue
        # One relation answers at most one requested output, the same rule the value-witness channel
        # holds; two paths on one tile means at least one of them is wrong.
        if index in claimed_candidates:
            continue
        candidate = opportunity.candidates[index]
        claimed_paths.add(path)
        claimed_candidates.add(index)
        reads.append({"output_path": path, "value_text": candidate.value_text, "label": candidate.label})
    return reads
