"""Pure lifecycle logic for persisted completion-criteria sets (SKY-10931).

A criteria set belongs to exactly one goal epoch of one chat. Sets are immutable
once written and superseded wholesale — never edited per criterion. Staleness
always degrades to regeneration, never to a sticky block (GOTCHAS §9). DB IO
stays at the route/repository seam; everything here is side-effect free.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Literal, cast, get_args

from skyvern.forge.sdk.copilot.completion_output_grounding import (
    _normalize_output_path,
    split_requested_output_criteria,
)
from skyvern.forge.sdk.copilot.completion_verification import (
    CompletionVerificationResult,
    run_plane_all_no_evidence,
)
from skyvern.forge.sdk.copilot.request_policy import (
    ANTECEDENT_FAMILY_VALUES,
    REQUESTED_OUTPUT_PATH_MINT_SOURCES,
    AntecedentFamily,
    CompletionCriterion,
    CriterionKind,
    ExpectedOutputShape,
    MintDisposition,
    Pinability,
    RequestedOutputEvidenceSource,
    RequestedOutputPathMintSource,
    TerminalActionFamily,
    TerminalActionVerificationMode,
    _canonical_bool_string,
    _coerce_classification_output_key,
    _coerce_expected_classification,
    _coerce_expected_output_shape,
    _coerce_expected_output_value,
    _coerce_judgment_truth_condition,
    _coerce_requested_output_evidence_source,
    _normalize_contingent_antecedent_output_path,
    _normalize_deliverable_confirmation_criterion_id,
    _normalize_deliverable_kind,
    is_defer_authoring_durable_fill_criterion,
    is_fallback_floor_criterion,
    is_neutral_reported_boolean_criterion,
    is_presence_only_requested_output_criterion,
    judgment_truth_condition_key,
    normalize_neutral_reported_boolean_criterion,
    normalized_criterion_outcome_key,
    requested_output_path_for_field,
    resolve_mint_degrade,
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
_TERMINAL_ACTION_VERIFICATION_MODES = frozenset(get_args(TerminalActionVerificationMode))
_MINT_DISPOSITIONS = frozenset(get_args(MintDisposition))
_PINABILITIES = frozenset(get_args(Pinability))


def _normalize_floor_rekeyed_association(marker: object, path: object) -> tuple[bool, str | None]:
    if marker is not True or not isinstance(path, str) or not path.strip().startswith("output."):
        return False, None
    normalized_path = _normalize_output_path(path)
    if normalized_path:
        return True, f"output.{normalized_path}"
    return False, None


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
        criterion = normalize_neutral_reported_boolean_criterion(criterion)
        item = {
            "id": criterion.id,
            "outcome": criterion.outcome,
            "contingent_on": criterion.contingent_on,
            "contingent_antecedent_output_path": criterion.contingent_antecedent_output_path,
            "deliverable_kind": criterion.deliverable_kind,
            "declared_deliverable_kind": criterion.declared_deliverable_kind,
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
        if criterion.deliverable_confirmation_criterion_id is not None:
            item["deliverable_confirmation_criterion_id"] = criterion.deliverable_confirmation_criterion_id
        if criterion.kind == "terminal_action":
            item["terminal_action_verification_mode"] = criterion.terminal_action_verification_mode
        if criterion.requested_output_corroborator:
            item["requested_output_corroborator"] = True
        if criterion.antecedent_family is not None:
            item["antecedent_family"] = criterion.antecedent_family
        if criterion.requested_output_path_mint_source is not None:
            item["requested_output_path_mint_source"] = criterion.requested_output_path_mint_source
        floor_rekeyed, floor_rekeyed_from_path = _normalize_floor_rekeyed_association(
            criterion.requested_output_floor_rekeyed,
            criterion.floor_rekeyed_from_path,
        )
        if floor_rekeyed:
            item["requested_output_floor_rekeyed"] = True
            item["floor_rekeyed_from_path"] = floor_rekeyed_from_path
        if criterion.mint_degrade is not None:
            item["mint_degrade"] = criterion.mint_degrade
        if criterion.judgment_truth_condition is not None:
            item["judgment_predicate"] = criterion.judgment_truth_condition.predicate
            item["judgment_polarity_when_holds"] = criterion.judgment_truth_condition.polarity_when_holds
        if criterion.request_slot_id is not None:
            item["request_slot_id"] = criterion.request_slot_id
        if criterion.pinability is not None:
            item["pinability"] = criterion.pinability
        if criterion.mint_disposition != "decidable":
            item["mint_disposition"] = criterion.mint_disposition
        items.append(item)
    return items


def criterion_authority_projection(
    criterion: CompletionCriterion,
    *,
    stored_item: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    """Canonical values for persisted fields that can change grading authority.

    Repository admission compares only fields present in a stored row, preserving
    omitted legacy defaults while rejecting values the tolerant decoder normalized.
    """
    criterion = normalize_neutral_reported_boolean_criterion(criterion)
    item = criteria_to_json([criterion])[0]
    floor_rekeyed, floor_rekeyed_from_path = _normalize_floor_rekeyed_association(
        criterion.requested_output_floor_rekeyed,
        criterion.floor_rekeyed_from_path,
    )
    item.update(
        {
            "antecedent_family": criterion.antecedent_family,
            "deliverable_confirmation_criterion_id": criterion.deliverable_confirmation_criterion_id,
            "terminal_action_verification_mode": criterion.terminal_action_verification_mode,
            "requested_output_corroborator": criterion.requested_output_corroborator,
            "requested_output_path_mint_source": criterion.requested_output_path_mint_source,
            "requested_output_floor_rekeyed": floor_rekeyed,
            "floor_rekeyed_from_path": floor_rekeyed_from_path,
            "mint_degrade": criterion.mint_degrade,
            "judgment_predicate": (
                criterion.judgment_truth_condition.predicate if criterion.judgment_truth_condition else None
            ),
            "judgment_polarity_when_holds": (
                criterion.judgment_truth_condition.polarity_when_holds if criterion.judgment_truth_condition else None
            ),
            "request_slot_id": criterion.request_slot_id,
            "pinability": criterion.pinability,
            "mint_disposition": criterion.mint_disposition,
        }
    )
    if (
        stored_item is not None
        and is_neutral_reported_boolean_criterion(criterion)
        and stored_item.get("requested_output_floor_rekeyed") is True
        and stored_item.get("floor_rekeyed_from_path") == f"output.{criterion.classification_output_key}"
    ):
        # The one historical coherent neutral tuple normalizes to the current unmarked tuple.
        # Preserve its exact marker pair only for stored-authority comparison so malformed
        # variants remain non-adoptable instead of being blessed by tolerant decoding.
        item["requested_output_floor_rekeyed"] = True
        item["floor_rekeyed_from_path"] = stored_item["floor_rekeyed_from_path"]
    return item


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
        contingent_on_raw = item.get("contingent_on")
        contingent_on = (
            contingent_on_raw.strip() if isinstance(contingent_on_raw, str) and contingent_on_raw.strip() else None
        )
        contingent_antecedent_output_path = _normalize_contingent_antecedent_output_path(
            item.get("contingent_antecedent_output_path")
        )
        antecedent_family_raw = item.get("antecedent_family")
        antecedent_family = cast(
            AntecedentFamily | None,
            antecedent_family_raw
            if isinstance(antecedent_family_raw, str) and antecedent_family_raw in ANTECEDENT_FAMILY_VALUES
            else None,
        )
        kind_raw = item.get("kind")
        kind = kind_raw if isinstance(kind_raw, str) and kind_raw in _CRITERION_KINDS else "outcome"
        family_raw = item.get("terminal_action_family")
        terminal_action_family = (
            family_raw if kind == "terminal_action" and family_raw in _TERMINAL_ACTION_FAMILIES else None
        )
        verification_mode_raw = item.get("terminal_action_verification_mode")
        terminal_action_verification_mode = (
            verification_mode_raw
            if kind == "terminal_action" and verification_mode_raw in _TERMINAL_ACTION_VERIFICATION_MODES
            else "family_record_v1"
        )
        mint_source_raw = item.get("requested_output_path_mint_source")
        requested_output_path_mint_source = cast(
            RequestedOutputPathMintSource | None,
            mint_source_raw if mint_source_raw in REQUESTED_OUTPUT_PATH_MINT_SOURCES else None,
        )
        request_slot_id_raw = item.get("request_slot_id")
        request_slot_id = (
            request_slot_id_raw
            if isinstance(request_slot_id_raw, str)
            and len(request_slot_id_raw) == 64
            and all(char in "0123456789abcdef" for char in request_slot_id_raw)
            else None
        )
        pinability_raw = item.get("pinability")
        pinability = pinability_raw if pinability_raw in _PINABILITIES else None
        mint_disposition_raw = item.get("mint_disposition")
        mint_disposition = mint_disposition_raw if mint_disposition_raw in _MINT_DISPOSITIONS else "decidable"
        requested_output_floor_rekeyed, floor_rekeyed_from_path = _normalize_floor_rekeyed_association(
            item.get("requested_output_floor_rekeyed"),
            item.get("floor_rekeyed_from_path"),
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
            # Typed boolean classifications carry their own validated mint metadata. Rows without
            # it carry no reliable shape, so retain the historical normalization for those rows.
            is_typed_boolean_classification = (
                stored_expected_output_shape == "goal_judgment_boolean"
                and pinability == "pinned"
                and mint_disposition == "pending"
            )
            if is_typed_boolean_classification:
                requested_output_evidence_source = "independent_run_evidence"
            else:
                stored_expected_output_shape = None
                requested_output_evidence_source = "runtime_output"
        elif isinstance(stored_expected_output_value, bool) or stored_expected_output_shape == "goal_judgment_boolean":
            requested_output_evidence_source = "independent_run_evidence"
        stored_level = level if isinstance(level, str) and level in _CRITERION_LEVELS else "run"
        stored_method_mandated = bool(item.get("method_mandated"))
        stored_deliverable_kind = _normalize_deliverable_kind(item.get("deliverable_kind"))
        deliverable_confirmation_criterion_id = _normalize_deliverable_confirmation_criterion_id(
            item.get("deliverable_confirmation_criterion_id")
        )
        if (
            stored_level != "run"
            or kind != "outcome"
            or stored_output_path is not None
            or stored_deliverable_kind is not None
            or stored_method_mandated
        ):
            deliverable_confirmation_criterion_id = None
        criterion = CompletionCriterion(
            id=criterion_id,
            outcome=outcome,
            contingent_on=contingent_on,
            contingent_antecedent_output_path=contingent_antecedent_output_path,
            antecedent_family=antecedent_family,
            deliverable_kind=stored_deliverable_kind,
            deliverable_confirmation_criterion_id=deliverable_confirmation_criterion_id,
            declared_deliverable_kind=_normalize_deliverable_kind(item.get("declared_deliverable_kind")),
            implicit=bool(item.get("implicit")),
            method_mandated=stored_method_mandated,
            level=stored_level,  # type: ignore[arg-type]
            output_path=stored_output_path,
            expected_output_value=stored_expected_output_value,
            expected_output_shape=stored_expected_output_shape,
            requested_output_evidence_source=cast(RequestedOutputEvidenceSource, requested_output_evidence_source),
            requested_output_path_mint_source=requested_output_path_mint_source,
            kind=cast(CriterionKind, kind),
            terminal_action_family=cast(TerminalActionFamily | None, terminal_action_family),
            terminal_action_verification_mode=cast(TerminalActionVerificationMode, terminal_action_verification_mode),
            classification_output_key=classification_output_key,
            expected_classification=expected_classification,
            requested_output_corroborator=bool(item.get("requested_output_corroborator")),
            requested_output_floor_rekeyed=requested_output_floor_rekeyed,
            floor_rekeyed_from_path=floor_rekeyed_from_path,
            mint_degrade=resolve_mint_degrade(
                item.get("mint_degrade"), contingent_on, contingent_antecedent_output_path
            ),
            judgment_truth_condition=_coerce_judgment_truth_condition(
                item.get("judgment_predicate"), item.get("judgment_polarity_when_holds")
            ),
            request_slot_id=request_slot_id,
            pinability=cast(Pinability | None, pinability),
            mint_disposition=cast(MintDisposition, mint_disposition),
        )
        criteria.append(normalize_neutral_reported_boolean_criterion(criterion))
    return tuple(criteria)


def _criterion_reconcile_key(criterion: CompletionCriterion) -> str:
    criterion = normalize_neutral_reported_boolean_criterion(criterion)
    contingent_key = criterion.contingent_on or ""
    contingent_path_key = criterion.contingent_antecedent_output_path or ""
    antecedent_family_key = criterion.antecedent_family or ""
    deliverable_kind_key = (
        f"{criterion.deliverable_kind or ''}\x1fdeclared:{criterion.declared_deliverable_kind or ''}"
        f"\x1fconfirmation:{criterion.deliverable_confirmation_criterion_id or ''}"
        f"\x1fjudgment:{judgment_truth_condition_key(criterion.judgment_truth_condition)}"
    )
    expected_output_value_key = typed_expected_output_value_key(criterion.expected_output_value)
    expected_output_shape_key = criterion.expected_output_shape or ""
    requested_output_evidence_source_key = criterion.requested_output_evidence_source
    terminal_action_verification_mode_key = (
        criterion.terminal_action_verification_mode if criterion.kind == "terminal_action" else ""
    )
    classification_output_key = criterion.classification_output_key or ""
    expected_classification_key = (
        str(criterion.expected_classification) if criterion.expected_classification is not None else ""
    )
    floor_rekeyed, floor_rekeyed_from_path = _normalize_floor_rekeyed_association(
        criterion.requested_output_floor_rekeyed,
        criterion.floor_rekeyed_from_path,
    )
    floor_rekeyed_key = (
        f"\x1ffloor_rekeyed:{str(floor_rekeyed).lower()}\x1ffloor_rekeyed_from_path:{floor_rekeyed_from_path or ''}"
    )
    neutral_reported_boolean_key = ""
    if is_neutral_reported_boolean_criterion(criterion):
        neutral_reported_boolean_key = (
            f"\x1fneutral_reported_boolean_key:{criterion.classification_output_key}"
            f"\x1fneutral_expected_output_shape:{criterion.expected_output_shape}"
            f"\x1fneutral_evidence_source:{criterion.requested_output_evidence_source}"
        )
    if criterion.output_path:
        return (
            f"contingent:{contingent_key}\x1fantecedent_path:{contingent_path_key}"
            f"\x1fantecedent_family:{antecedent_family_key}"
            f"\x1fdeliverable_kind:{deliverable_kind_key}"
            f"\x1foutput_path:{criterion.output_path}"
            f"\x1fexpected_output_value:{expected_output_value_key}"
            f"\x1fexpected_output_shape:{expected_output_shape_key}"
            f"\x1frequested_output_evidence_source:{requested_output_evidence_source_key}"
            f"\x1fkind:{criterion.kind}"
            f"\x1fterminal_action_verification_mode:{terminal_action_verification_mode_key}"
            f"\x1fclassification_output_key:{classification_output_key}"
            f"\x1fexpected_classification:{expected_classification_key}"
            f"{floor_rekeyed_key}"
        )
    if criterion.kind == "validation_classification":
        return (
            f"contingent:{contingent_key}\x1fantecedent_path:{contingent_path_key}"
            f"\x1fantecedent_family:{antecedent_family_key}"
            f"\x1fdeliverable_kind:{deliverable_kind_key}"
            f"\x1fkind:{criterion.kind}"
            f"\x1fclassification_output_key:{classification_output_key}"
            f"\x1fexpected_classification:{expected_classification_key}"
            f"{floor_rekeyed_key}"
        )
    return (
        f"contingent:{contingent_key}\x1fantecedent_path:{contingent_path_key}"
        f"\x1fantecedent_family:{antecedent_family_key}"
        f"\x1fdeliverable_kind:{deliverable_kind_key}"
        f"\x1fkind:{criterion.kind}"
        f"\x1fterminal_action_verification_mode:{terminal_action_verification_mode_key}"
        f"\x1foutcome:{normalized_criterion_outcome_key(criterion.outcome)}"
        f"{neutral_reported_boolean_key}"
        f"{floor_rekeyed_key}"
    )


def _outcome_key_set(criteria: tuple[CompletionCriterion, ...] | list[CompletionCriterion]) -> set[str]:
    return {_criterion_reconcile_key(criterion) for criterion in criteria}


def _preserve_stored_terminal_action_authority(
    fresh: list[CompletionCriterion], stored: tuple[CompletionCriterion, ...]
) -> list[CompletionCriterion]:
    """Carry semantic terminal-action authority into a fresh set before deciding its epoch."""
    stored_semantic_terminal_actions = [
        criterion
        for criterion in stored
        if criterion.kind == "terminal_action" and criterion.terminal_action_verification_mode == "semantic_outcome_v1"
    ]
    preserved: list[CompletionCriterion] = []
    for criterion in fresh:
        matches = []
        for stored_criterion in stored_semantic_terminal_actions:
            if criterion.kind not in {"outcome", "terminal_action"}:
                continue
            promoted = replace(
                criterion,
                kind="terminal_action",
                terminal_action_family=stored_criterion.terminal_action_family,
                terminal_action_verification_mode="semantic_outcome_v1",
            )
            if _criterion_reconcile_key(promoted) == _criterion_reconcile_key(stored_criterion):
                matches.append(promoted)
        if len(matches) == 1:
            criterion = matches[0]
        preserved.append(criterion)
    return preserved


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


def apply_requested_output_producer_floor(
    criteria: Iterable[CompletionCriterion],
) -> tuple[tuple[CompletionCriterion, ...], tuple[str, ...]]:
    """Re-key presence-only requested-output criteria (no expected value, shape, or deliverable) to a
    run-plane outcome so the observed-end-state judge grades them instead of failing closed. Typed
    value/shape criteria, judgment booleans, and typed deliverables are untouched; the transform is idempotent."""
    criteria = tuple(criteria)
    requested, _remaining = split_requested_output_criteria(list(criteria))
    presence_only_ids = {
        criterion.id for criterion in requested if is_presence_only_requested_output_criterion(criterion)
    }
    if not presence_only_ids:
        return criteria, ()
    floored: list[CompletionCriterion] = []
    rekeyed_paths: list[str] = []
    for criterion in criteria:
        if criterion.id in presence_only_ids:
            rekeyed_paths.append(criterion.output_path or "")
            floored.append(
                replace(
                    criterion,
                    output_path=None,
                    level="run",
                    kind="outcome",
                    requested_output_floor_rekeyed=True,
                    floor_rekeyed_from_path=criterion.floor_rekeyed_from_path or criterion.output_path,
                )
            )
        else:
            floored.append(criterion)
    return tuple(floored), tuple(rekeyed_paths)


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
    fresh = [criterion for criterion in fresh if not is_defer_authoring_durable_fill_criterion(criterion)]
    if fresh and all(is_fallback_floor_criterion(criterion) for criterion in fresh):
        fresh = []
    if stored is None:
        if not fresh:
            return ReconcileDecision(action="none", reason="no_criteria", epoch=0, criteria=())
        if not actionable:
            return ReconcileDecision(action="none", reason="not_actionable", epoch=0, criteria=tuple(fresh))
        return ReconcileDecision(
            action="create",
            reason="first",
            epoch=next_epoch,
            criteria=tuple(fresh),
        )
    if not fresh:
        return ReconcileDecision(
            action="adopt_stored",
            reason="empty_fresh",
            epoch=stored.goal_epoch,
            criteria=stored.criteria,
        )
    fresh = _preserve_stored_terminal_action_authority(fresh, stored.criteria)
    if _outcome_key_set(fresh) <= _outcome_key_set(stored.criteria):
        return ReconcileDecision(
            action="adopt_stored",
            reason="kept",
            epoch=stored.goal_epoch,
            criteria=stored.criteria,
        )
    if _fresh_generic_rephrase_lacks_stored_requested_outputs(
        stored.criteria,
        fresh,
        requested_output_path_aliases=requested_output_path_aliases,
    ):
        return ReconcileDecision(
            action="adopt_stored",
            reason="kept",
            epoch=stored.goal_epoch,
            criteria=stored.criteria,
        )
    if not actionable:
        return ReconcileDecision(
            action="adopt_stored",
            reason="not_actionable",
            epoch=stored.goal_epoch,
            criteria=stored.criteria,
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
