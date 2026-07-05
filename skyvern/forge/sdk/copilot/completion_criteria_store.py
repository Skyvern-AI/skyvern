"""Pure lifecycle logic for persisted completion-criteria sets (SKY-10931).

A criteria set belongs to exactly one goal epoch of one chat. Sets are immutable
once written and superseded wholesale — never edited per criterion. Staleness
always degrades to regeneration, never to a sticky block (GOTCHAS §9). DB IO
stays at the route/repository seam; everything here is side-effect free.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, cast

from skyvern.forge.sdk.copilot.completion_verification import (
    CompletionVerificationResult,
    run_plane_all_no_evidence,
)
from skyvern.forge.sdk.copilot.request_policy import (
    CompletionCriterion,
    CriterionKind,
    ExpectedOutputShape,
    RequestedOutputEvidenceSource,
    TerminalActionFamily,
    _canonical_bool_string,
    _coerce_classification_output_key,
    _coerce_expected_classification,
    _coerce_expected_output_shape,
    _coerce_expected_output_value,
    _coerce_requested_output_evidence_source,
    _normalize_contingent_antecedent_output_path,
    _normalize_deliverable_kind,
    is_fallback_floor_criterion,
    normalized_criterion_outcome_key,
    requested_output_path_for_field,
    typed_expected_output_value_key,
)

ReconcileAction = Literal["create", "adopt_stored", "none"]
ReconcileReason = Literal["first", "not_subset", "kept", "empty_fresh", "no_criteria", "not_actionable"]
SupersedeReason = Literal["not_subset", "tripwire"]

CRITERIA_SET_STATUS_ACTIVE = "active"
CRITERIA_SET_STATUS_SUPERSEDED = "superseded"
TRIPWIRE_CONSECUTIVE_ALL_NO_EVIDENCE = 2
_CRITERION_LEVELS = ("definition", "run")
_CRITERION_KINDS = ("outcome", "terminal_action", "validation_classification")
_TERMINAL_ACTION_FAMILIES = ("request", "application", "form", "order")


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
    items: list[dict[str, Any]] = []
    for criterion in criteria:
        item = {
            "id": criterion.id,
            "outcome": criterion.outcome,
            "contingent_on": criterion.contingent_on,
            "contingent_antecedent_output_path": criterion.contingent_antecedent_output_path,
            "deliverable_kind": criterion.deliverable_kind,
            "implicit": criterion.implicit,
            "method_mandated": criterion.method_mandated,
            "level": criterion.level,
            "output_path": criterion.output_path,
            "expected_output_value": criterion.expected_output_value,
            "expected_output_shape": criterion.expected_output_shape,
            "requested_output_evidence_source": criterion.requested_output_evidence_source,
            "kind": criterion.kind,
            "terminal_action_family": criterion.terminal_action_family,
            "classification_output_key": criterion.classification_output_key,
            "expected_classification": criterion.expected_classification,
        }
        if criterion.requested_output_corroborator:
            item["requested_output_corroborator"] = True
        items.append(item)
    return items


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
        output_path = item.get("output_path")
        expected_output_value = item.get("expected_output_value")
        expected_output_shape = _coerce_expected_output_shape(item.get("expected_output_shape"))
        requested_output_evidence_source = _coerce_requested_output_evidence_source(
            item.get("requested_output_evidence_source")
        )
        classification_output_key = _coerce_classification_output_key(item.get("classification_output_key"))
        expected_classification = _coerce_expected_classification(item.get("expected_classification"))
        contingent_on = item.get("contingent_on")
        contingent_antecedent_output_path = _normalize_contingent_antecedent_output_path(
            item.get("contingent_antecedent_output_path")
        )
        kind_raw = item.get("kind")
        kind = kind_raw if isinstance(kind_raw, str) and kind_raw in _CRITERION_KINDS else "outcome"
        family_raw = item.get("terminal_action_family")
        terminal_action_family = (
            family_raw if kind == "terminal_action" and family_raw in _TERMINAL_ACTION_FAMILIES else None
        )
        stored_output_path = output_path.strip() if isinstance(output_path, str) and output_path.strip() else None
        stored_expected_output_value = _coerce_expected_output_value(expected_output_value)
        stored_expected_output_shape = cast(ExpectedOutputShape | None, expected_output_shape)
        if isinstance(stored_expected_output_value, str) and (
            requested_output_evidence_source == "independent_run_evidence"
            or stored_expected_output_shape == "goal_judgment_boolean"
        ):
            coerced_judgment_bool = _canonical_bool_string(stored_expected_output_value)
            if coerced_judgment_bool is not None:
                stored_expected_output_value = coerced_judgment_bool
        if kind == "validation_classification":
            stored_output_path = None
            stored_expected_output_value = None
            stored_expected_output_shape = None
            requested_output_evidence_source = "runtime_output"
        elif isinstance(stored_expected_output_value, bool) or stored_expected_output_shape == "goal_judgment_boolean":
            requested_output_evidence_source = "independent_run_evidence"
        criteria.append(
            CompletionCriterion(
                id=criterion_id,
                outcome=outcome,
                contingent_on=contingent_on.strip()
                if isinstance(contingent_on, str) and contingent_on.strip()
                else None,
                contingent_antecedent_output_path=contingent_antecedent_output_path,
                deliverable_kind=_normalize_deliverable_kind(item.get("deliverable_kind")),
                implicit=bool(item.get("implicit")),
                method_mandated=bool(item.get("method_mandated")),
                level=level if isinstance(level, str) and level in _CRITERION_LEVELS else "run",  # type: ignore[arg-type]
                output_path=stored_output_path,
                expected_output_value=stored_expected_output_value,
                expected_output_shape=stored_expected_output_shape,
                requested_output_evidence_source=cast(RequestedOutputEvidenceSource, requested_output_evidence_source),
                kind=cast(CriterionKind, kind),
                terminal_action_family=cast(TerminalActionFamily | None, terminal_action_family),
                classification_output_key=classification_output_key,
                expected_classification=expected_classification,
                requested_output_corroborator=bool(item.get("requested_output_corroborator")),
            )
        )
    return tuple(criteria)


def _criterion_reconcile_key(criterion: CompletionCriterion) -> str:
    contingent_key = criterion.contingent_on or ""
    contingent_path_key = criterion.contingent_antecedent_output_path or ""
    deliverable_kind_key = criterion.deliverable_kind or ""
    expected_output_value_key = typed_expected_output_value_key(criterion.expected_output_value)
    expected_output_shape_key = criterion.expected_output_shape or ""
    requested_output_evidence_source_key = criterion.requested_output_evidence_source
    classification_output_key = criterion.classification_output_key or ""
    expected_classification_key = (
        str(criterion.expected_classification) if criterion.expected_classification is not None else ""
    )
    if criterion.output_path:
        return (
            f"contingent:{contingent_key}\x1fantecedent_path:{contingent_path_key}"
            f"\x1fdeliverable_kind:{deliverable_kind_key}"
            f"\x1foutput_path:{criterion.output_path}"
            f"\x1fexpected_output_value:{expected_output_value_key}"
            f"\x1fexpected_output_shape:{expected_output_shape_key}"
            f"\x1frequested_output_evidence_source:{requested_output_evidence_source_key}"
            f"\x1fkind:{criterion.kind}"
            f"\x1fclassification_output_key:{classification_output_key}"
            f"\x1fexpected_classification:{expected_classification_key}"
        )
    if criterion.kind == "validation_classification":
        return (
            f"contingent:{contingent_key}\x1fantecedent_path:{contingent_path_key}"
            f"\x1fdeliverable_kind:{deliverable_kind_key}"
            f"\x1fkind:{criterion.kind}"
            f"\x1fclassification_output_key:{classification_output_key}"
            f"\x1fexpected_classification:{expected_classification_key}"
        )
    return (
        f"contingent:{contingent_key}\x1fantecedent_path:{contingent_path_key}"
        f"\x1fdeliverable_kind:{deliverable_kind_key}"
        f"\x1fkind:{criterion.kind}"
        f"\x1foutcome:{normalized_criterion_outcome_key(criterion.outcome)}"
    )


def _outcome_key_set(criteria: tuple[CompletionCriterion, ...] | list[CompletionCriterion]) -> set[str]:
    return {_criterion_reconcile_key(criterion) for criterion in criteria}


def _word_tokens(text: str) -> list[str]:
    return "".join(char if char.isalnum() else " " for char in text.casefold()).split()


_REQUESTED_OUTPUT_WORDS = frozenset(
    "capture captured extract extracted final include included includes output read record result return returned".split()
)
_REQUESTED_OUTPUT_FIELD_TOKENS = frozenset(
    "address addresses date dates email emails id identifier identifiers license licenses location locations name names "
    "npi number numbers owner owners phone phones result specialties specialty status statuses taxonomy".split()
)
_GENERIC_PROFILE_MARKERS = (
    "profile details",
    "profile information",
    "profile is captured",
    "profile is extracted",
    "intended end state",
    "expected output",
)


def _requested_output_tokens(criteria: tuple[CompletionCriterion, ...] | list[CompletionCriterion]) -> set[str]:
    tokens: set[str] = set()
    for criterion in criteria:
        if criterion.level == "definition" or criterion.method_mandated:
            continue
        outcome_tokens = _word_tokens(criterion.outcome)
        if not any(word in _REQUESTED_OUTPUT_WORDS for word in outcome_tokens):
            continue
        for token in outcome_tokens:
            if token in _REQUESTED_OUTPUT_FIELD_TOKENS:
                tokens.add(token)
    return tokens


def requested_output_paths(criteria: tuple[CompletionCriterion, ...] | list[CompletionCriterion]) -> set[str]:
    return {
        criterion.output_path
        for criterion in criteria
        if criterion.output_path and criterion.level != "definition" and not criterion.method_mandated
    }


def _criterion_mentions_output_path(
    criterion: CompletionCriterion,
    output_path: str,
    aliases: dict[str, str] | None = None,
) -> bool:
    outcome_tokens = _word_tokens(criterion.outcome)
    max_span_len = min(4, len(outcome_tokens))
    for span_len in range(max_span_len, 0, -1):
        for start in range(len(outcome_tokens) - span_len + 1):
            field_name = " ".join(outcome_tokens[start : start + span_len])
            if requested_output_path_for_field(field_name, aliases) == output_path:
                return True
    return False


def _is_generic_profile_criterion(criterion: CompletionCriterion) -> bool:
    key = normalized_criterion_outcome_key(criterion.outcome)
    return is_fallback_floor_criterion(criterion) or any(marker in key for marker in _GENERIC_PROFILE_MARKERS)


def _fresh_generic_rephrase_lacks_stored_requested_outputs(
    stored: tuple[CompletionCriterion, ...],
    fresh: list[CompletionCriterion],
    *,
    requested_output_path_aliases: dict[str, str] | None = None,
) -> bool:
    stored_requested_criteria = tuple(
        criterion
        for criterion in stored
        if criterion.output_path and criterion.level != "definition" and not criterion.method_mandated
    )
    stored_requested_paths = {
        output_path for criterion in stored_requested_criteria if (output_path := criterion.output_path) is not None
    }
    if stored_requested_paths:
        fresh_requested_paths = requested_output_paths(fresh)
        missing_paths = stored_requested_paths - fresh_requested_paths
        if not missing_paths:
            return False
        if fresh and all(_is_generic_profile_criterion(criterion) for criterion in fresh):
            return True
        return any(
            criterion.output_path is None
            and _criterion_mentions_output_path(criterion, output_path, requested_output_path_aliases)
            for criterion in fresh
            for output_path in missing_paths
        )

    stored_requested_tokens = _requested_output_tokens(stored)
    if not stored_requested_tokens:
        return False
    if not fresh or not all(_is_generic_profile_criterion(criterion) for criterion in fresh):
        return False
    fresh_tokens = set().union(*(_word_tokens(criterion.outcome) for criterion in fresh))
    return bool(stored_requested_tokens - fresh_tokens)


def reconcile_completion_criteria(
    snapshot: StoredCriteriaSnapshot | None,
    fresh: list[CompletionCriterion],
    *,
    actionable: bool,
    requested_output_path_aliases: dict[str, str] | None = None,
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
    if _fresh_generic_rephrase_lacks_stored_requested_outputs(
        stored.criteria,
        fresh,
        requested_output_path_aliases=requested_output_path_aliases,
    ):
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
