"""Pure lifecycle logic for persisted completion-criteria sets (SKY-10931).

A criteria set belongs to exactly one goal epoch of one chat. Sets are immutable
once written and superseded wholesale — never edited per criterion. Staleness
always degrades to regeneration, never to a sticky block (GOTCHAS §9). DB IO
stays at the route/repository seam; everything here is side-effect free.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from skyvern.forge.sdk.copilot.completion_verification import (
    CompletionVerificationResult,
    run_plane_all_no_evidence,
)
from skyvern.forge.sdk.copilot.request_policy import (
    CompletionCriterion,
    is_fallback_floor_criterion,
    normalized_criterion_outcome_key,
)

ReconcileAction = Literal["create", "adopt_stored", "none"]
ReconcileReason = Literal["first", "not_subset", "kept", "empty_fresh", "no_criteria", "not_actionable"]
SupersedeReason = Literal["not_subset", "tripwire"]

CRITERIA_SET_STATUS_ACTIVE = "active"
CRITERIA_SET_STATUS_SUPERSEDED = "superseded"
TRIPWIRE_CONSECUTIVE_ALL_NO_EVIDENCE = 2
_CRITERION_LEVELS = ("definition", "run")


@dataclass(frozen=True)
class StoredCriteriaSet:
    set_id: str
    goal_epoch: int
    criteria: tuple[CompletionCriterion, ...]
    consecutive_all_no_evidence: int = 0
    tripwire_fired: bool = False
    last_fully_satisfied_workflow_yaml: str | None = None


@dataclass(frozen=True)
class StoredCriteriaSnapshot:
    """What the route loads before the turn: the active set (if any) and the next
    free epoch (max persisted epoch + 1, so epochs stay monotonic across supersedes)."""

    active: StoredCriteriaSet | None = None
    next_epoch: int = 1


@dataclass(frozen=True)
class ReconcileDecision:
    action: ReconcileAction
    reason: ReconcileReason
    epoch: int
    criteria: tuple[CompletionCriterion, ...]
    superseded_set_id: str | None = None

    def to_trace_data(self) -> dict[str, Any]:
        return {
            "criteria_lifecycle_action": self.action,
            "criteria_lifecycle_reason": self.reason,
            "criteria_lifecycle_epoch": self.epoch,
            "criteria_lifecycle_count": len(self.criteria),
        }


@dataclass
class CompletionCriteriaTurnState:
    """Mutable per-turn record connecting the reconcile decision to post-turn
    persistence: adjudication events accumulate as run/observation verifications
    land, and the route folds them into the stored row after the turn."""

    decision: ReconcileDecision | None = None
    active_set_id: str | None = None
    prior_consecutive_all_no_evidence: int = 0
    prior_tripwire_fired: bool = False
    known_good_yaml_available: bool = False
    adjudication_all_no_evidence_events: list[bool] = field(default_factory=list)
    fully_satisfied_workflow_yaml: str | None = None
    last_verdict_state_counts: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class PersistencePlan:
    """What the route must write after the turn. ``counter_*`` applies to the row
    named by ``counter_set_id`` — the adopted stored row, or the row created this
    turn (``counter_set_id is None`` then; the route uses the new row's id)."""

    create_epoch: int | None = None
    create_criteria: tuple[CompletionCriterion, ...] = ()
    create_reason: ReconcileReason | None = None
    supersede_set_id: str | None = None
    supersede_reason: SupersedeReason | None = None
    counter_set_id: str | None = None
    counter_value: int = 0
    tripwire_fired: bool = False
    fully_satisfied_workflow_yaml: str | None = None

    @property
    def creates_set(self) -> bool:
        return self.create_epoch is not None


def criteria_to_json(criteria: tuple[CompletionCriterion, ...] | list[CompletionCriterion]) -> list[dict[str, Any]]:
    return [
        {
            "id": criterion.id,
            "outcome": criterion.outcome,
            "implicit": criterion.implicit,
            "method_mandated": criterion.method_mandated,
            "level": criterion.level,
        }
        for criterion in criteria
    ]


def criteria_from_json(raw: Any) -> tuple[CompletionCriterion, ...]:
    if not isinstance(raw, list):
        return ()
    criteria: list[CompletionCriterion] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        criterion_id = item.get("id")
        outcome = item.get("outcome")
        if not isinstance(criterion_id, str) or not isinstance(outcome, str) or not outcome.strip():
            continue
        level = item.get("level")
        criteria.append(
            CompletionCriterion(
                id=criterion_id,
                outcome=outcome,
                implicit=bool(item.get("implicit")),
                method_mandated=bool(item.get("method_mandated")),
                level=level if isinstance(level, str) and level in _CRITERION_LEVELS else "run",  # type: ignore[arg-type]
            )
        )
    return tuple(criteria)


def _outcome_key_set(criteria: tuple[CompletionCriterion, ...] | list[CompletionCriterion]) -> set[str]:
    return {normalized_criterion_outcome_key(criterion.outcome) for criterion in criteria}


def reconcile_completion_criteria(
    snapshot: StoredCriteriaSnapshot | None,
    fresh: list[CompletionCriterion],
    *,
    actionable: bool,
) -> ReconcileDecision:
    """Decide once per turn whether the stored set survives or a new epoch begins.

    An empty fresh derivation never supersedes (repair/run follow-up turns commonly
    derive nothing); a fresh key set that subsets the stored keys keeps the stored
    set; anything else supersedes wholesale. Non-actionable (clarification) turns
    never create or supersede.
    """
    stored = snapshot.active if snapshot is not None else None
    next_epoch = snapshot.next_epoch if snapshot is not None else 1
    if fresh and all(is_fallback_floor_criterion(criterion) for criterion in fresh):
        fresh = []
    if stored is None:
        if not fresh:
            return ReconcileDecision(action="none", reason="no_criteria", epoch=0, criteria=())
        if not actionable:
            return ReconcileDecision(action="none", reason="not_actionable", epoch=0, criteria=tuple(fresh))
        return ReconcileDecision(action="create", reason="first", epoch=next_epoch, criteria=tuple(fresh))
    if not fresh:
        return ReconcileDecision(
            action="adopt_stored", reason="empty_fresh", epoch=stored.goal_epoch, criteria=stored.criteria
        )
    if _outcome_key_set(fresh) <= _outcome_key_set(stored.criteria):
        return ReconcileDecision(
            action="adopt_stored", reason="kept", epoch=stored.goal_epoch, criteria=stored.criteria
        )
    if not actionable:
        return ReconcileDecision(
            action="adopt_stored", reason="not_actionable", epoch=stored.goal_epoch, criteria=stored.criteria
        )
    return ReconcileDecision(
        action="create",
        reason="not_subset",
        epoch=next_epoch,
        criteria=tuple(fresh),
        superseded_set_id=stored.set_id,
    )


def build_turn_state(
    snapshot: StoredCriteriaSnapshot | None, decision: ReconcileDecision
) -> CompletionCriteriaTurnState:
    stored = snapshot.active if snapshot is not None else None
    if decision.action != "adopt_stored" or stored is None:
        return CompletionCriteriaTurnState(decision=decision)
    return CompletionCriteriaTurnState(
        decision=decision,
        active_set_id=stored.set_id,
        prior_consecutive_all_no_evidence=stored.consecutive_all_no_evidence,
        prior_tripwire_fired=stored.tripwire_fired,
        known_good_yaml_available=bool(stored.last_fully_satisfied_workflow_yaml),
    )


def note_adjudication_on_turn_state(
    turn_state: CompletionCriteriaTurnState | None,
    verification: CompletionVerificationResult,
    *,
    fully_satisfied_workflow_yaml: str | None = None,
) -> None:
    if turn_state is None or verification.status != "evaluated":
        return
    turn_state.adjudication_all_no_evidence_events.append(run_plane_all_no_evidence(verification))
    turn_state.last_verdict_state_counts = verification.verdict_state_counts()
    turn_state.fully_satisfied_workflow_yaml = (
        fully_satisfied_workflow_yaml if verification.is_fully_satisfied() and fully_satisfied_workflow_yaml else None
    )


def plan_persistence(turn_state: CompletionCriteriaTurnState | None) -> PersistencePlan | None:
    """Fold the turn's reconcile decision and adjudication events into the writes
    the route must make. The tripwire fires at most once per epoch: when the
    consecutive all-no_evidence counter reaches the threshold on a set that has
    not fired yet, the set is superseded so the next derivation starts a new epoch."""
    if turn_state is None or turn_state.decision is None:
        return None
    decision = turn_state.decision
    if decision.action == "none":
        return None

    if decision.action == "create":
        # A set created this turn folds its counter but can never fire: the
        # tripwire detects persisted staleness, which needs at least one
        # adopted turn to exist. Early all-no_evidence runs while the workflow
        # is still being built must not churn fresh epochs.
        counter = 0
        for all_no_evidence in turn_state.adjudication_all_no_evidence_events:
            counter = counter + 1 if all_no_evidence else 0
        return PersistencePlan(
            create_epoch=decision.epoch,
            create_criteria=decision.criteria,
            create_reason=decision.reason,
            supersede_set_id=decision.superseded_set_id,
            supersede_reason="not_subset" if decision.superseded_set_id else None,
            counter_set_id=None,
            counter_value=counter,
            tripwire_fired=False,
            fully_satisfied_workflow_yaml=turn_state.fully_satisfied_workflow_yaml,
        )

    if not turn_state.adjudication_all_no_evidence_events and turn_state.fully_satisfied_workflow_yaml is None:
        return None

    counter = turn_state.prior_consecutive_all_no_evidence
    fired = turn_state.prior_tripwire_fired
    fired_now = False
    for all_no_evidence in turn_state.adjudication_all_no_evidence_events:
        counter = counter + 1 if all_no_evidence else 0
        if counter >= TRIPWIRE_CONSECUTIVE_ALL_NO_EVIDENCE and not fired:
            fired = True
            fired_now = True

    return PersistencePlan(
        supersede_set_id=turn_state.active_set_id if fired_now else None,
        supersede_reason="tripwire" if fired_now else None,
        counter_set_id=turn_state.active_set_id,
        counter_value=counter,
        tripwire_fired=fired,
        fully_satisfied_workflow_yaml=turn_state.fully_satisfied_workflow_yaml,
    )
