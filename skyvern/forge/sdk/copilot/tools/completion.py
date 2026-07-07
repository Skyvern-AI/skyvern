import time
from collections.abc import Mapping
from typing import Any, Protocol

import structlog

from skyvern.config import settings
from skyvern.forge.sdk.copilot.completion_criteria_store import note_adjudication_on_turn_state
from skyvern.forge.sdk.copilot.completion_output_grounding import (
    grade_requested_output_criteria,
    split_requested_output_criteria,
)
from skyvern.forge.sdk.copilot.completion_verification import (
    _FALLBACK_FLOOR_CARRIER_SOURCES,
    _STRUCTURED_RECORD_CRITERION_IDS,
    CompletionVerificationResult,
    CriterionVerdict,
    EvidenceSourceKind,
    RunEvidenceSnapshot,
    _contingent_metadata_for_criteria,
    _is_structural_requested_output_abstention,
    carry_degraded_criterion_ids,
    combine_verification_results,
    evaluate_completion_criteria,
    grade_definition_criteria,
    grade_fallback_floor_reached_end_state_criteria,
    grade_present_value_criteria,
    grade_record_semantic_consistency,
    grade_registered_download_criteria,
    grade_structured_record_criteria,
    grade_terminal_goal_record_corroboration,
    grade_terminal_goal_record_criteria,
    grade_validation_classification_criteria,
    is_fallback_floor_base_criterion,
    is_registered_download_completion_criterion,
    only_degraded_blocking,
    registered_download_completion_criterion,
    structural_unfired_contingent_criterion_ids,
    summarize_unsatisfied_outcomes,
    verdict_missing_evidence,
)
from skyvern.forge.sdk.copilot.enforcement import _goal_likely_needs_more_blocks
from skyvern.forge.sdk.copilot.llm_config import resolve_main_copilot_handler
from skyvern.forge.sdk.copilot.outcome_verification_trace import record_completion_verification
from skyvern.forge.sdk.copilot.output_utils import iter_failure_reasons
from skyvern.forge.sdk.copilot.reached_download_target import (
    DOWNLOAD_KIND_ATTRIBUTE,
    DOWNLOAD_KIND_EXTENSION,
    DOWNLOAD_KIND_REGISTERED,
    REGISTERED_DOWNLOAD_OUTPUT_KEYS,
    ReachedDownloadTarget,
    derive_from_block_outputs,
)
from skyvern.forge.sdk.copilot.request_policy import CompletionCriterion, is_fallback_floor_criterion
from skyvern.forge.sdk.copilot.runtime import PreRunPageReference, RegisteredArtifactEvidence
from skyvern.forge.sdk.copilot.terminal_predicates import outcome_fully_verified
from skyvern.forge.sdk.copilot.tracing_setup import copilot_span

from ._shared import (
    RUN_BLOCKS_SAFETY_CEILING_SECONDS,
    _copilot_seconds_remaining,
    _current_workflow_block_labels,
    _current_workflow_has_evidence_block,
    _failed_run_block_labels,
    _is_meaningful_extracted_data,
    _registered_output_parameter_payloads,
    _valid_runtime_anchor_url,
    _workflow_output_parameter_payloads,
)
from .blockers import (
    _active_run_terminal_evidence_detected,
    _analyze_run_blocks,
    _looks_like_anti_bot_blocker,
    _run_blocks_structured_blocker_message,
)

LOG = structlog.get_logger()

_TYPED_DOWNLOAD_KINDS = frozenset({DOWNLOAD_KIND_REGISTERED, DOWNLOAD_KIND_ATTRIBUTE, DOWNLOAD_KIND_EXTENSION})
_POST_RUN_PAGE_OBSERVATION_LABEL = "post_run_page_observation"
_REGISTERED_ARTIFACT_OBSERVATION_LABEL = "registered_artifact_observation"
# Stamp keys the same-run gate reads; they are dropped from the graded payload so the run id
# and observation flag cannot be traversed as observed page content.
_POST_RUN_PAGE_EVIDENCE_STAMP_KEYS = frozenset({"workflow_run_id", "observed_after_workflow_run"})
_REGISTERED_DOWNLOAD_REQUESTED_OUTPUT_PATHS = frozenset(f"output.{key}" for key in REGISTERED_DOWNLOAD_OUTPUT_KEYS)
_AUTHORED_OUTPUT_CONTRACT_CRITERION_ID_PREFIX = "__copilot_authored_output__"
_AUTHORED_OUTPUT_CONTRACT_MISSING_CRITERION_ID = "__copilot_authored_output_contract_missing"
_AUTHORED_OUTPUT_CONTRACT_MISSING_PATH = "output.__copilot_missing_authored_output_contract__"
_VALIDATION_REVIEW_OUTPUT_CONTRACT_HINT = (
    " For validation-only pre-submit Review pages, do not repair by returning only booleans such as "
    "pre_submit_review_reached, submit_control_visible, submit_or_finalize_clicked, or per-field *_verified flags. "
    "The Review block output must include an explicit validation-only marker such as `validation_only: true` or "
    '`submit_mode: "validation_only"`, `review_values` or `review_fields` as visible Review-page label/value '
    "strings, `evidence_text` containing the visible Review-page text that verbatim contains those values, and an "
    "explicit false submit/finalize-click signal. Stop on the Review page; do not click Submit/Finalize."
)


def _completion_request_policy(copilot_ctx: Any) -> Any | None:
    try:
        return copilot_ctx.request_policy
    except AttributeError:
        return None


def _is_typed_download_target(value: object) -> bool:
    if isinstance(value, ReachedDownloadTarget):
        return value.download_kind in _TYPED_DOWNLOAD_KINDS
    if not isinstance(value, dict):
        return False
    download_kind = value.get("download_kind")
    return isinstance(download_kind, str) and download_kind in _TYPED_DOWNLOAD_KINDS


def _ctx_reached_download_target(copilot_ctx: Any) -> object | None:
    try:
        return copilot_ctx.reached_download_target
    except AttributeError:
        return None


def _result_data(result: dict[str, Any]) -> dict[str, Any]:
    data = result.get("data")
    return data if isinstance(data, dict) else {}


def _result_block_outputs_by_label(result: dict[str, Any]) -> dict[str, Any]:
    data = _result_data(result)
    blocks = data.get("blocks")
    block_outputs: dict[str, Any] = {}
    if isinstance(blocks, list):
        for block in blocks:
            if not isinstance(block, dict):
                continue
            label = block.get("label")
            output = block.get("extracted_data")
            if isinstance(label, str) and isinstance(output, dict):
                block_outputs[label] = output
    # Signal detection reads raw download keys; evidence snapshots normalize/redact payloads before grading.
    for registered in _registered_output_parameter_payloads(data):
        label = registered.get("output_parameter_key") or registered.get("block_label")
        value = registered.get("value")
        if isinstance(label, str) and isinstance(value, dict):
            block_outputs[label] = value
    return block_outputs


def _result_has_registered_download_block_output(result: dict[str, Any]) -> bool:
    return derive_from_block_outputs(_result_block_outputs_by_label(result)) is not None


def _registered_download_requested_output_criterion(criterion: CompletionCriterion) -> bool:
    return (
        criterion.output_path is not None
        and criterion.level != "definition"
        and not criterion.method_mandated
        and criterion.output_path in _REGISTERED_DOWNLOAD_REQUESTED_OUTPUT_PATHS
    )


def _has_typed_download_signal(copilot_ctx: Any, result: dict[str, Any]) -> bool:
    if _is_typed_download_target(_ctx_reached_download_target(copilot_ctx)):
        return True
    if _is_typed_download_target(_result_data(result).get("reached_download_target")):
        return True
    return _result_has_registered_download_block_output(result)


def _reconcile_download_completion_criterion(
    copilot_ctx: Any, result: dict[str, Any], criteria: list[CompletionCriterion]
) -> list[CompletionCriterion]:
    has_registered_download_evidence = _result_has_registered_download_block_output(result)
    reconciled = (
        [criterion for criterion in criteria if not _registered_download_requested_output_criterion(criterion)]
        if has_registered_download_evidence
        else criteria
    )
    if any(is_registered_download_completion_criterion(criterion) for criterion in reconciled):
        return reconciled
    if not _has_typed_download_signal(copilot_ctx, result):
        return reconciled
    return [*reconciled, registered_download_completion_criterion()]


def _completion_verification_criteria(copilot_ctx: Any) -> list[CompletionCriterion]:
    policy = _completion_request_policy(copilot_ctx)
    # A method-mandated criterion asserts HOW the goal was reached; the outcome
    # judge sees only end-state evidence.
    criteria = policy.graded_completion_criteria() if policy is not None else []
    authored_output_criteria = _authored_output_contract_criteria(copilot_ctx)
    if authored_output_criteria and (not criteria or all(is_fallback_floor_criterion(c) for c in criteria)):
        return authored_output_criteria
    if _accepted_staged_output_contract_missing(copilot_ctx) and (
        not criteria or all(is_fallback_floor_criterion(c) for c in criteria)
    ):
        return [_authored_output_contract_missing_criterion()]
    return criteria


def _authored_output_contract_criteria(copilot_ctx: Any) -> list[CompletionCriterion]:
    paths = _authored_output_contract_paths(copilot_ctx)
    return [
        CompletionCriterion(
            id=f"{_AUTHORED_OUTPUT_CONTRACT_CRITERION_ID_PREFIX}{path.replace('[]', '_items').replace('.', '_')}",
            outcome=f"The run output includes the authored output contract path {path}.",
            implicit=True,
            level="run",
            output_path=path,
        )
        for path in paths
    ]


def _authored_output_contract_paths(copilot_ctx: Any) -> list[str]:
    if _accepted_staged_proposal_present(copilot_ctx):
        return sorted(_authored_output_contract_metadata_paths(_accepted_staged_output_contract_metadata(copilot_ctx)))
    repair_context_paths = _authored_output_contract_repair_context_paths(copilot_ctx)
    if repair_context_paths:
        return sorted(repair_context_paths)
    return sorted(_authored_output_contract_metadata_paths(_accepted_staged_output_contract_metadata(copilot_ctx)))


def _accepted_staged_output_contract_metadata(copilot_ctx: Any) -> object:
    evidence = getattr(copilot_ctx, "workflow_verification_evidence", None)
    metadata = getattr(evidence, "code_artifact_metadata", None)
    if _authored_output_contract_metadata_paths(metadata):
        return metadata
    return getattr(copilot_ctx, "code_artifact_metadata", None)


def _authored_output_contract_metadata_paths(metadata: object) -> set[str]:
    paths: set[str] = set()
    if isinstance(metadata, Mapping):
        for artifact in metadata.values():
            if not isinstance(artifact, Mapping):
                continue
            for row_group_key in ("claimed_outcomes", "terminal_verifier_expectations"):
                row_group = artifact.get(row_group_key)
                if not isinstance(row_group, list):
                    continue
                for row in row_group:
                    if not isinstance(row, Mapping):
                        continue
                    paths.update(_authored_output_contract_paths_from_list(row.get("goal_value_paths")))
    return paths


def _accepted_staged_output_contract_missing(copilot_ctx: Any) -> bool:
    return (
        _accepted_staged_proposal_present(copilot_ctx)
        and not _authored_output_contract_paths(copilot_ctx)
        and bool(_authored_output_contract_repair_context_paths(copilot_ctx))
    )


def _accepted_staged_proposal_present(copilot_ctx: Any) -> bool:
    return bool(getattr(copilot_ctx, "has_staged_proposal", False) or getattr(copilot_ctx, "staged_workflow", None))


def _authored_output_contract_missing_criterion() -> CompletionCriterion:
    return CompletionCriterion(
        id=_AUTHORED_OUTPUT_CONTRACT_MISSING_CRITERION_ID,
        outcome="The accepted staged workflow exposes an authored output contract.",
        implicit=True,
        level="run",
        output_path=_AUTHORED_OUTPUT_CONTRACT_MISSING_PATH,
    )


def _authored_output_contract_repair_context_paths(copilot_ctx: Any) -> set[str]:
    repair_context = getattr(copilot_ctx, "last_code_authoring_repair_context", None)
    paths: set[str] = set()
    for attr in ("required_goal_value_paths", "required_extraction_schema_paths", "required_code_return_paths"):
        paths.update(_authored_output_contract_paths_from_list(getattr(repair_context, attr, None)))
    return paths


def _authored_output_contract_paths_from_list(value: object) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {path for raw_path in value for path in [_authored_output_contract_path(raw_path)] if path}


def _authored_output_contract_path(value: object) -> str:
    if not isinstance(value, str):
        return ""
    path = value.strip()
    if path == "output." or path == "output":
        return ""
    if path.startswith("output."):
        return path
    if path.startswith("_") or not path.replace("_", "").isalnum():
        return ""
    return f"output.{path}"


def _split_criteria_by_plane(criteria: list[Any]) -> tuple[list[CompletionCriterion], list[CompletionCriterion]]:
    run_criteria = [c for c in criteria if getattr(c, "level", "run") != "definition"]
    definition_criteria = [c for c in criteria if getattr(c, "level", "run") == "definition"]
    return run_criteria, definition_criteria


def _classifier_status(copilot_ctx: Any) -> str:
    policy = getattr(copilot_ctx, "request_policy", None)
    return policy.classifier_status if policy is not None else "not_run"


def _no_gradeable_run_plane_result(criterion_ids: list[str]) -> CompletionVerificationResult:
    return CompletionVerificationResult(
        status="evaluated",
        criterion_ids=list(criterion_ids),
        verdicts=[
            CriterionVerdict(criterion_id=criterion_id, state="unknown", reason_code="unknown")
            for criterion_id in criterion_ids
        ],
        no_gradeable_run_plane=True,
    )


def _definition_plane_workflow_yaml(copilot_ctx: Any) -> str | None:
    last_yaml = getattr(copilot_ctx, "last_workflow_yaml", None)
    if isinstance(last_yaml, str) and last_yaml.strip():
        return last_yaml
    initial_yaml = getattr(copilot_ctx, "workflow_yaml", None)
    return initial_yaml if isinstance(initial_yaml, str) else None


def _record_adjudication_on_turn_state(copilot_ctx: Any, verification: CompletionVerificationResult | None) -> None:
    if verification is None or verification.status != "evaluated":
        return
    note_adjudication_on_turn_state(
        getattr(copilot_ctx, "completion_criteria_turn_state", None),
        verification,
        fully_satisfied_workflow_yaml=_definition_plane_workflow_yaml(copilot_ctx)
        if verification.is_fully_satisfied()
        else None,
    )


def _verified_context_block_labels_for_snapshot(
    copilot_ctx: Any, current_labels: list[str], executed_labels: list[str]
) -> list[str]:
    current_label_set = set(current_labels)
    executed_label_set = set(executed_labels)
    candidates: set[str] = set()
    for raw_values in (
        getattr(copilot_ctx, "verified_prefix_labels", None),
        getattr(getattr(copilot_ctx, "workflow_verification_evidence", None), "block_verified", None),
    ):
        if not isinstance(raw_values, list):
            continue
        candidates.update(str(label) for label in raw_values if isinstance(label, str) and label in current_label_set)
    return [label for label in current_labels if label in candidates and label not in executed_label_set]


def _build_page_observation_evidence_snapshot(
    copilot_ctx: Any,
    *,
    url: str,
    title: str = "",
    observed_data: object | None = None,
) -> RunEvidenceSnapshot:
    run_id = getattr(copilot_ctx, "last_run_blocks_workflow_run_id", None)
    block_outputs: dict[str, Any] = {}
    block_output_sources: dict[str, EvidenceSourceKind] = {}
    if isinstance(observed_data, dict) and observed_data:
        block_outputs["current_page_observation"] = observed_data
        block_output_sources["current_page_observation"] = "independent_page_evidence"
    elif observed_data is not None:
        block_outputs["current_page_observation"] = str(observed_data)
        block_output_sources["current_page_observation"] = "independent_page_evidence"
    return RunEvidenceSnapshot(
        workflow_run_id=run_id if isinstance(run_id, str) else None,
        block_outputs=block_outputs,
        block_output_sources=block_output_sources,
        current_url=_valid_runtime_anchor_url(url),
        page_title=title if isinstance(title, str) and title.strip() else None,
    )


async def _maybe_run_completion_verification_from_page_observation(
    copilot_ctx: Any,
    *,
    url: str,
    title: str = "",
    observed_data: object | None = None,
) -> CompletionVerificationResult | None:
    """Verify completion only for post-run page observations after failed tests."""

    existing = getattr(copilot_ctx, "completion_verification_result", None)
    if isinstance(existing, CompletionVerificationResult) and existing.is_fully_satisfied():
        return existing
    if getattr(copilot_ctx, "post_run_page_observation_after_failed_test", False) is not True:
        return None
    criteria = _completion_verification_criteria(copilot_ctx)
    run_criteria, definition_criteria = _split_criteria_by_plane(criteria)
    criterion_ids = [criterion.id for criterion in criteria]
    contingent_ids, contingent_on_by_id, contingent_path_by_id = _contingent_metadata_for_criteria(criteria)
    run_contingent_ids, run_contingent_on_by_id, run_contingent_path_by_id = _contingent_metadata_for_criteria(
        run_criteria
    )
    if _classifier_status(copilot_ctx) == "fallback" and not run_criteria:
        verification = _no_gradeable_run_plane_result(criterion_ids)
        verification = carry_degraded_criterion_ids(verification, criteria)
        copilot_ctx.completion_verification_result = verification
        record_completion_verification(copilot_ctx, verification)
        _record_adjudication_on_turn_state(copilot_ctx, verification)
        _emit_completion_verification_trace(copilot_ctx, verification)
        return verification
    if not criteria:
        return None
    definition_verdicts = (
        grade_definition_criteria(definition_criteria, _definition_plane_workflow_yaml(copilot_ctx))
        if definition_criteria
        else []
    )
    if not run_criteria:
        verification = combine_verification_results(
            criterion_ids,
            None,
            definition_verdicts,
            contingent_criterion_ids=contingent_ids,
            contingent_on_by_criterion_id=contingent_on_by_id,
            contingent_antecedent_output_path_by_criterion_id=contingent_path_by_id,
        )
    else:
        snapshot = _build_page_observation_evidence_snapshot(
            copilot_ctx,
            url=url,
            title=title,
            observed_data=observed_data,
        )
        run_structural_unfired_ids = structural_unfired_contingent_criterion_ids(run_criteria, snapshot)
        requested_output_criteria, judgeable_run_criteria = split_requested_output_criteria(run_criteria)
        requested_output_verdicts = (
            grade_requested_output_criteria(copilot_ctx, requested_output_criteria, snapshot)
            if requested_output_criteria
            else []
        )
        validation_classification_verdicts = grade_validation_classification_criteria(judgeable_run_criteria, snapshot)
        validation_classification_ids = {verdict.criterion_id for verdict in validation_classification_verdicts}
        remaining_judgeable_run_criteria = [
            criterion for criterion in judgeable_run_criteria if criterion.id not in validation_classification_ids
        ]
        remaining = _copilot_seconds_remaining(copilot_ctx)
        if (
            remaining is not None
            and remaining
            <= settings.COPILOT_COMPLETION_JUDGE_TIMEOUT_SECONDS + _COMPLETION_VERIFICATION_BUDGET_MARGIN_SECONDS
        ):
            run_result = (
                _merge_run_verdicts(
                    run_criteria,
                    requested_output_verdicts,
                    validation_classification_verdicts,
                    structural_unfired_criterion_ids=run_structural_unfired_ids,
                )
                if requested_output_verdicts or validation_classification_verdicts
                else None
            )
            verification = (
                combine_verification_results(
                    criterion_ids,
                    run_result,
                    definition_verdicts,
                    contingent_criterion_ids=contingent_ids,
                    contingent_on_by_criterion_id=contingent_on_by_id,
                    contingent_antecedent_output_path_by_criterion_id=contingent_path_by_id,
                )
                if run_result is not None
                else CompletionVerificationResult(
                    status="unavailable",
                    criterion_ids=criterion_ids,
                    contingent_criterion_ids=contingent_ids,
                    contingent_on_by_criterion_id=contingent_on_by_id,
                    contingent_antecedent_output_path_by_criterion_id=contingent_path_by_id,
                    structural_unfired_criterion_ids=run_structural_unfired_ids,
                )
            )
        else:
            if not snapshot.has_evidence():
                run_result = CompletionVerificationResult(
                    status="evaluated",
                    criterion_ids=[criterion.id for criterion in run_criteria],
                    verdicts=requested_output_verdicts
                    + validation_classification_verdicts
                    + [
                        CriterionVerdict(criterion_id=criterion.id, state="unsatisfied", reason_code="no_evidence")
                        for criterion in remaining_judgeable_run_criteria
                    ],
                    contingent_criterion_ids=run_contingent_ids,
                    contingent_on_by_criterion_id=run_contingent_on_by_id,
                    contingent_antecedent_output_path_by_criterion_id=run_contingent_path_by_id,
                    structural_unfired_criterion_ids=run_structural_unfired_ids,
                )
            elif not remaining_judgeable_run_criteria:
                run_result = _merge_run_verdicts(
                    run_criteria,
                    requested_output_verdicts,
                    validation_classification_verdicts,
                    structural_unfired_criterion_ids=run_structural_unfired_ids,
                )
            else:
                handler = await _completion_verification_handler(copilot_ctx)
                if handler is None:
                    run_result = (
                        _merge_run_verdicts(
                            run_criteria,
                            requested_output_verdicts,
                            validation_classification_verdicts,
                            structural_unfired_criterion_ids=run_structural_unfired_ids,
                        )
                        if requested_output_verdicts or validation_classification_verdicts
                        else None
                    )
                    if run_result is None:
                        return None
                else:
                    judgeable_result = await evaluate_completion_criteria(
                        remaining_judgeable_run_criteria,
                        snapshot,
                        handler,
                    )
                    judgeable_result = _apply_present_value_upgrades(
                        judgeable_result,
                        remaining_judgeable_run_criteria,
                        snapshot,
                    )
                    if judgeable_result.status != "evaluated":
                        deterministic_result = (
                            _merge_run_verdicts(
                                run_criteria,
                                requested_output_verdicts,
                                validation_classification_verdicts,
                                structural_unfired_criterion_ids=run_structural_unfired_ids,
                            )
                            if requested_output_verdicts or validation_classification_verdicts
                            else None
                        )
                        run_result = deterministic_result or judgeable_result
                    else:
                        run_result = _merge_run_verdicts(
                            run_criteria,
                            requested_output_verdicts,
                            validation_classification_verdicts,
                            judgeable_result.verdicts,
                            structural_unfired_criterion_ids=run_structural_unfired_ids,
                        )
            verification = combine_verification_results(
                criterion_ids,
                run_result,
                definition_verdicts,
                contingent_criterion_ids=contingent_ids,
                contingent_on_by_criterion_id=contingent_on_by_id,
                contingent_antecedent_output_path_by_criterion_id=contingent_path_by_id,
            )

    verification = carry_degraded_criterion_ids(verification, criteria)
    if (
        isinstance(existing, CompletionVerificationResult)
        and not verification.is_fully_satisfied()
        and not (existing.status == "unavailable" and verification.status == "evaluated")
    ):
        return existing

    copilot_ctx.completion_verification_result = verification
    record_completion_verification(copilot_ctx, verification)
    _record_adjudication_on_turn_state(copilot_ctx, verification)
    if verification.status == "evaluated":
        _emit_completion_verification_trace(copilot_ctx, verification)
    return verification


_COMPLETION_VERIFICATION_BUDGET_MARGIN_SECONDS = 5.0
_DOWNLOAD_EVIDENCE_FILE_NAME_KEYS = ("filename", "file_name", "name", "path")
_MAX_EVIDENCE_FILE_NAMES = 5


def _download_file_name(value: Any) -> str | None:
    if isinstance(value, dict):
        for key in _DOWNLOAD_EVIDENCE_FILE_NAME_KEYS:
            if name := _download_file_name(value.get(key)):
                return name
        return None
    if not isinstance(value, str) or not value.strip():
        return None
    name = value.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1].strip()
    return name or None


def _completion_evidence_payload(output: Any) -> Any:
    if not isinstance(output, dict):
        return output
    nested_output = output.get("output")
    has_download_output = any(output.get(key) for key in REGISTERED_DOWNLOAD_OUTPUT_KEYS)
    has_nested_download_output = isinstance(nested_output, dict) and any(
        nested_output.get(key) for key in REGISTERED_DOWNLOAD_OUTPUT_KEYS
    )
    if not has_download_output and not has_nested_download_output:
        return output
    download_output = output if has_download_output or not isinstance(nested_output, dict) else nested_output
    names: list[str] = []
    if name := _download_file_name(download_output.get("downloaded_file_name")):
        names.append(name)
    files = download_output.get("downloaded_files")
    if isinstance(files, list):
        names.extend(name for item in files[:_MAX_EVIDENCE_FILE_NAMES] if (name := _download_file_name(item)))
    urls = download_output.get("downloaded_file_urls")
    if isinstance(urls, list):
        names.extend(name for item in urls[:_MAX_EVIDENCE_FILE_NAMES] if (name := _download_file_name(item)))
    payload: dict[str, Any] = {"download_registered": True}
    if isinstance(files, list):
        payload["downloaded_file_count"] = len(files)
    if isinstance(urls, list):
        payload["downloaded_file_url_count"] = len(urls)
    artifacts = download_output.get("downloaded_file_artifact_ids")
    if isinstance(artifacts, list):
        payload["downloaded_file_artifact_count"] = len(artifacts)
    if names:
        payload["downloaded_file_names"] = list(dict.fromkeys(names))[:_MAX_EVIDENCE_FILE_NAMES]
    if has_nested_download_output and not has_download_output and isinstance(nested_output, dict):
        preserved_output = {
            key: value
            for key, value in nested_output.items()
            if key not in REGISTERED_DOWNLOAD_OUTPUT_KEYS and key not in {"page", "download"}
        }
        if preserved_output:
            payload["output"] = preserved_output
    return payload


_ARTIFACT_HEALTH_EXCLUDED_CATEGORIES = frozenset(
    {
        "ACTIVE_RUN_TERMINAL_EVIDENCE",
        "ANTI_BOT_DETECTION",
        "AUTH_FAILURE",
        "BROWSER_ERROR",
        "CREDENTIAL_ERROR",
        "INFRASTRUCTURE_ERROR",
        "NAVIGATION_FAILURE",
        "PER_TOOL_BUDGET",
        "PROXY_ERROR",
    }
)

_TYPE_ERROR_GENERATED_CALL_MARKERS = (
    "wait_for_function",
    "positional argument",
    "positional arguments",
    "got an unexpected keyword",
    "missing 1 required",
)


async def _completion_verification_handler(copilot_ctx: Any) -> Any | None:
    return await resolve_main_copilot_handler(
        getattr(copilot_ctx, "workflow_permanent_id", None),
        getattr(copilot_ctx, "organization_id", None),
    )


def _is_outcome_evidence_candidate(copilot_ctx: Any, result: dict[str, Any]) -> bool:
    """A clean ok=True run worth judging on its whole-workflow outcome.

    Recognition is governed by the outcome evidence the user can observe, not by
    whether the run produced any data. The judge requires positive evidence for
    every criterion, so a genuinely-empty run still grades unsatisfied; only a
    terminal anti-bot blocker keeps a completed run out of the judge, because that
    evidence makes the apparent success unusable.
    """
    if not bool(result.get("ok", False)):
        return False
    structured_blocker = _run_blocks_structured_blocker_message(result, copilot_ctx)
    anti_bot, _empty_data_blocks, _categories = _analyze_run_blocks(result, copilot_ctx)
    if structured_blocker and (anti_bot or _looks_like_anti_bot_blocker(structured_blocker)):
        return False
    return True


def _is_unfinished_run_verification_candidate(copilot_ctx: Any, result: dict[str, Any]) -> bool:
    """A canceled/partial run (ok=False) still worth judging because it left runtime
    evidence behind. The judge confirms a criterion only on positive evidence, so a
    broken run never spuriously passes; this only lets a reached goal be recognized
    even though the run did not finish cleanly — recognition must not key on run status.
    """
    if bool(result.get("ok", False)):
        return False
    if _active_run_terminal_evidence_detected(result):
        return False
    if _run_blocks_structured_blocker_message(result, copilot_ctx):
        return False
    data = result.get("data")
    if not isinstance(data, dict):
        return False
    return _valid_runtime_anchor_url(data.get("current_url")) is not None


def _failure_category_names(result: dict[str, Any]) -> list[str]:
    data = result.get("data")
    data = data if isinstance(data, dict) else {}
    raw_categories = data.get("failure_categories")
    if not isinstance(raw_categories, list):
        return []
    categories: list[str] = []
    for item in raw_categories:
        if not isinstance(item, dict):
            continue
        category = item.get("category")
        if isinstance(category, str) and category.strip():
            categories.append(category.strip())
    return list(dict.fromkeys(categories))


def _artifact_health_failure_class(reason: str) -> str | None:
    lowered = reason.lower()
    if "syntaxerror" in lowered:
        return "SyntaxError"
    if "nameerror" in lowered:
        return "NameError"
    if "typeerror" in lowered and any(marker in lowered for marker in _TYPE_ERROR_GENERATED_CALL_MARKERS):
        return "TypeError"
    return None


def _artifact_health_blocker_from_result(
    result: dict[str, Any], failure_reasons: list[str] | None = None
) -> tuple[str | None, list[str], list[str]]:
    if bool(result.get("ok", False)):
        return None, [], []
    categories = _failure_category_names(result)
    if categories and all(category in _ARTIFACT_HEALTH_EXCLUDED_CATEGORIES for category in categories):
        return None, [], []

    failure_reasons = list(
        dict.fromkeys(failure_reasons if failure_reasons is not None else iter_failure_reasons(result))
    )
    failure_classes = [
        failure_class
        for failure_class in dict.fromkeys(
            _artifact_health_failure_class(reason) for reason in failure_reasons if isinstance(reason, str)
        )
        if failure_class is not None
    ]
    if not failure_classes:
        return None, [], []

    data = result.get("data")
    data = data if isinstance(data, dict) else {}
    failed_labels = _failed_run_block_labels(data)
    label_detail = f" in block(s) {', '.join(failed_labels)}" if failed_labels else ""
    reason_preview = " ".join(str(failure_reasons[0]).split())[:240] if failure_reasons else "unknown failure"
    reason = (
        f"Artifact-health blocker{label_detail}: deterministic generated-code/runtime "
        f"{'/'.join(failure_classes)} failure: {reason_preview}"
    )
    return reason, failed_labels, failure_classes


class _PostRunPageEvidenceCtx(Protocol):
    composition_page_evidence: Mapping[str, Any] | None


def _same_run_post_run_page_evidence(
    copilot_ctx: _PostRunPageEvidenceCtx, run_id: str | None
) -> Mapping[str, Any] | None:
    """Post-run page evidence stamped for the graded run, admitted only when its own
    ``workflow_run_id`` matches and it was observed after the run, so a stale pre-run page cannot certify."""
    if not isinstance(run_id, str) or not run_id:
        return None
    evidence = copilot_ctx.composition_page_evidence
    if not isinstance(evidence, Mapping):
        return None
    if evidence.get("observed_after_workflow_run") is not True:
        return None
    if evidence.get("workflow_run_id") != run_id:
        return None
    return evidence


def _bind_independent_post_run_page_evidence(
    copilot_ctx: _PostRunPageEvidenceCtx,
    run_id: str | None,
    block_outputs: dict[str, Any],
    block_output_sources: dict[str, EvidenceSourceKind],
) -> None:
    if _POST_RUN_PAGE_OBSERVATION_LABEL in block_outputs:
        return
    evidence = _same_run_post_run_page_evidence(copilot_ctx, run_id)
    if evidence is None:
        return
    block_outputs[_POST_RUN_PAGE_OBSERVATION_LABEL] = {
        key: value for key, value in evidence.items() if key not in _POST_RUN_PAGE_EVIDENCE_STAMP_KEYS
    }
    block_output_sources[_POST_RUN_PAGE_OBSERVATION_LABEL] = "independent_page_evidence"


def _bind_registered_artifact_evidence(
    evidence: RegisteredArtifactEvidence | None,
    run_id: str | None,
    block_outputs: dict[str, Any],
    block_output_sources: dict[str, EvidenceSourceKind],
) -> None:
    if _REGISTERED_ARTIFACT_OBSERVATION_LABEL in block_outputs:
        return
    if not isinstance(run_id, str) or not run_id:
        return
    if evidence is None or evidence.workflow_run_id != run_id or not evidence.entries:
        return
    block_outputs[_REGISTERED_ARTIFACT_OBSERVATION_LABEL] = {
        "parsed_text": " ".join(entry.parsed_text for entry in evidence.entries),
        "file_names": [entry.file_name for entry in evidence.entries],
    }
    block_output_sources[_REGISTERED_ARTIFACT_OBSERVATION_LABEL] = "registered_artifact_content"


def _pre_run_page_reference_text(reference: PreRunPageReference | None, run_id: str | None) -> str | None:
    if not isinstance(run_id, str) or not run_id:
        return None
    if reference is None or reference.workflow_run_id != run_id:
        return None
    return reference.text or None


def _build_run_evidence_snapshot(copilot_ctx: Any, result: dict[str, Any]) -> RunEvidenceSnapshot:
    data = result.get("data")
    data = data if isinstance(data, dict) else {}
    current_label_order = _current_workflow_block_labels(copilot_ctx)
    current_labels = set(current_label_order)
    # Evidence must be what THIS run produced. ``verified_block_outputs`` accumulates
    # across incremental runs, so sourcing from it would let an output from a prior
    # run satisfy a criterion the current run never re-produced.
    blocks = data.get("blocks")
    block_outputs: dict[str, Any] = {}
    block_output_sources: dict[str, EvidenceSourceKind] = {}
    if isinstance(blocks, list):
        for block in blocks:
            if not isinstance(block, dict):
                continue
            label = block.get("label")
            output = block.get("extracted_data")
            evidence_output = _completion_evidence_payload(output)
            if isinstance(label, str) and label in current_labels and _is_meaningful_extracted_data(evidence_output):
                block_outputs[label] = evidence_output
                block_output_sources[label] = "runtime_output"
            for output_key, output_value in _workflow_output_parameter_payloads(output).items():
                block_outputs[output_key] = output_value
                block_output_sources[output_key] = "registered_output_parameter"
    for output_key, output_value in _workflow_output_parameter_payloads(data.get("output")).items():
        block_outputs[output_key] = output_value
        block_output_sources[output_key] = "registered_output_parameter"
    for registered in _registered_output_parameter_payloads(data):
        registered_output_key = registered.get("output_parameter_key")
        registered_output_value = _completion_evidence_payload(registered.get("value"))
        registered_block_label = registered.get("block_label")
        if isinstance(registered_output_key, str) and registered_output_key:
            block_outputs[registered_output_key] = registered_output_value
            block_output_sources[registered_output_key] = "registered_output_parameter"
        if isinstance(registered_block_label, str) and registered_block_label in current_labels:
            if isinstance(registered_output_key, str) and registered_output_key:
                existing = block_outputs.get(registered_block_label)
                if isinstance(existing, dict):
                    existing.setdefault(registered_output_key, registered_output_value)
                else:
                    block_outputs[registered_block_label] = {registered_output_key: registered_output_value}
                block_output_sources.setdefault(registered_block_label, "registered_output_parameter")
            else:
                block_outputs[registered_block_label] = registered_output_value
                block_output_sources[registered_block_label] = "registered_output_parameter"
    run_id = data.get("workflow_run_id")
    _bind_independent_post_run_page_evidence(
        copilot_ctx, run_id if isinstance(run_id, str) else None, block_outputs, block_output_sources
    )
    registered_artifact_evidence = getattr(copilot_ctx, "registered_artifact_evidence", None)
    pre_run_page_reference = getattr(copilot_ctx, "pre_run_page_reference", None)
    _bind_registered_artifact_evidence(
        registered_artifact_evidence if isinstance(registered_artifact_evidence, RegisteredArtifactEvidence) else None,
        run_id if isinstance(run_id, str) else None,
        block_outputs,
        block_output_sources,
    )
    executed = data.get("executed_block_labels")
    executed_block_labels = [str(label) for label in executed] if isinstance(executed, list) else []
    page_title = data.get("page_title")
    run_terminal_status = data.get("overall_status")
    failure_reasons = [" ".join(reason.split()) for reason in iter_failure_reasons(result)]
    _artifact_reason, artifact_failed_labels, artifact_failure_classes = _artifact_health_blocker_from_result(
        result,
        failure_reasons=failure_reasons,
    )
    failed_block_labels = artifact_failed_labels or _failed_run_block_labels(data)
    return RunEvidenceSnapshot(
        workflow_run_id=run_id if isinstance(run_id, str) else None,
        block_outputs=block_outputs,
        block_output_sources=block_output_sources,
        current_url=_valid_runtime_anchor_url(data.get("current_url")),
        page_title=page_title if isinstance(page_title, str) and page_title.strip() else None,
        run_terminal_status=run_terminal_status
        if isinstance(run_terminal_status, str) and run_terminal_status
        else None,
        executed_block_labels=executed_block_labels,
        verified_context_block_labels=_verified_context_block_labels_for_snapshot(
            copilot_ctx,
            current_label_order,
            executed_block_labels,
        ),
        failed_block_labels=failed_block_labels,
        failure_classes=artifact_failure_classes,
        failure_reasons=failure_reasons,
        pre_run_page_reference_text=_pre_run_page_reference_text(
            pre_run_page_reference if isinstance(pre_run_page_reference, PreRunPageReference) else None,
            run_id if isinstance(run_id, str) else None,
        ),
    )


def _carrier_floor_verdicts(
    requested_output_verdicts: list[CriterionVerdict],
) -> tuple[CriterionVerdict, ...]:
    return tuple(
        verdict
        for verdict in requested_output_verdicts
        if verdict.state == "satisfied"
        and verdict.reason_code == "evidence_confirms"
        and verdict.evidence_source in _FALLBACK_FLOOR_CARRIER_SOURCES
    )


def _apply_present_value_upgrades(
    run_result: CompletionVerificationResult,
    run_criteria: list[CompletionCriterion],
    snapshot: RunEvidenceSnapshot,
    *,
    include_terminal_goal_records: bool = False,
    carrier_verdicts: tuple[CriterionVerdict, ...] = (),
) -> CompletionVerificationResult:
    """Upgrade a ``no_evidence``/``unknown`` run verdict to a deterministic present-value
    ``satisfied``. An ``evidence_contradicts`` verdict is left to the judge, a judge
    ``satisfied`` is never downgraded, and an unavailable result is never fabricated.
    """
    if run_result.status != "evaluated":
        return run_result
    upgrades = {verdict.criterion_id: verdict for verdict in grade_present_value_criteria(run_criteria, snapshot)}
    upgrades.update(
        {verdict.criterion_id: verdict for verdict in grade_structured_record_criteria(run_criteria, snapshot)}
    )
    upgrades.update(
        {verdict.criterion_id: verdict for verdict in grade_validation_classification_criteria(run_criteria, snapshot)}
    )
    if include_terminal_goal_records:
        upgrades.update(
            {
                verdict.criterion_id: verdict
                for verdict in grade_fallback_floor_reached_end_state_criteria(
                    run_criteria, snapshot, carrier_verdicts=carrier_verdicts
                )
            }
        )
        upgrades.update(
            {verdict.criterion_id: verdict for verdict in grade_terminal_goal_record_criteria(run_criteria, snapshot)}
        )
    semantic_verdicts = {
        verdict.criterion_id: verdict for verdict in grade_record_semantic_consistency(run_criteria, snapshot)
    }
    if not upgrades and not semantic_verdicts:
        return run_result

    def _merged(verdict: CriterionVerdict) -> CriterionVerdict:
        # Semantic contradictions are stronger than earlier satisfied verdicts:
        # a row-mixing/status contradiction means the record evidence is invalid.
        if verdict.criterion_id in semantic_verdicts:
            return semantic_verdicts[verdict.criterion_id]
        if verdict.criterion_id in upgrades and not verdict.satisfied and verdict.reason_code != "evidence_contradicts":
            return upgrades[verdict.criterion_id]
        return verdict

    verdicts = [_merged(verdict) for verdict in run_result.verdicts]
    return CompletionVerificationResult(
        status="evaluated",
        criterion_ids=list(run_result.criterion_ids),
        verdicts=verdicts,
        contingent_criterion_ids=list(run_result.contingent_criterion_ids),
        contingent_on_by_criterion_id=dict(run_result.contingent_on_by_criterion_id),
        contingent_antecedent_output_path_by_criterion_id=dict(
            run_result.contingent_antecedent_output_path_by_criterion_id
        ),
        structural_unfired_criterion_ids=list(run_result.structural_unfired_criterion_ids),
    )


def _merge_run_verdicts(
    run_criteria: list[CompletionCriterion],
    *verdict_groups: list[CriterionVerdict],
    contingent_criterion_ids: list[str] | None = None,
    contingent_on_by_criterion_id: dict[str, str] | None = None,
    contingent_antecedent_output_path_by_criterion_id: dict[str, str] | None = None,
    structural_unfired_criterion_ids: list[str] | None = None,
) -> CompletionVerificationResult:
    verdict_by_id: dict[str, CriterionVerdict] = {}
    for verdicts in verdict_groups:
        verdict_by_id.update({verdict.criterion_id: verdict for verdict in verdicts})
    default_contingent_ids, default_contingent_on_by_id, default_contingent_path_by_id = (
        _contingent_metadata_for_criteria(run_criteria)
    )
    criterion_ids = [criterion.id for criterion in run_criteria]
    return CompletionVerificationResult(
        status="evaluated",
        criterion_ids=criterion_ids,
        verdicts=[
            verdict_by_id.get(criterion_id, CriterionVerdict(criterion_id, "unknown", "unknown"))
            for criterion_id in criterion_ids
        ]
        + [
            verdict
            for verdicts in verdict_groups
            for verdict in verdicts
            if verdict.criterion_id not in criterion_ids
            and verdict.state == "satisfied"
            and verdict.reason_code == "evidence_confirms"
            and verdict.grounding_mode == "terminal_record"
        ],
        contingent_criterion_ids=contingent_criterion_ids or default_contingent_ids,
        contingent_on_by_criterion_id=contingent_on_by_criterion_id or default_contingent_on_by_id,
        contingent_antecedent_output_path_by_criterion_id=(
            contingent_antecedent_output_path_by_criterion_id or default_contingent_path_by_id
        ),
        structural_unfired_criterion_ids=structural_unfired_criterion_ids or [],
    )


def _merge_run_verdicts_if_requested_output_exists(
    run_criteria: list[CompletionCriterion],
    requested_output_verdicts: list[CriterionVerdict],
    *verdict_groups: list[CriterionVerdict],
    snapshot: RunEvidenceSnapshot | None = None,
    structural_unfired_criterion_ids: list[str] | None = None,
) -> CompletionVerificationResult | None:
    if not requested_output_verdicts:
        return None
    extra_verdicts = [grade_terminal_goal_record_corroboration(snapshot)] if snapshot is not None else []
    return _merge_run_verdicts(
        run_criteria,
        requested_output_verdicts,
        *verdict_groups,
        *extra_verdicts,
        structural_unfired_criterion_ids=structural_unfired_criterion_ids,
    )


def _filter_judged_fallback_floor_satisfaction(
    run_criteria: list[CompletionCriterion],
    judged_result: CompletionVerificationResult,
    deterministic_result: CompletionVerificationResult | None,
) -> list[CriterionVerdict]:
    deterministic_satisfied_ids = {
        verdict.criterion_id
        for verdict in (deterministic_result.verdicts if deterministic_result is not None else [])
        if verdict.satisfied
    }
    fallback_floor_ids = {criterion.id for criterion in run_criteria if is_fallback_floor_base_criterion(criterion)}
    verdicts: list[CriterionVerdict] = []
    for verdict in judged_result.verdicts:
        if verdict.criterion_id in fallback_floor_ids and verdict.satisfied:
            if verdict.criterion_id in deterministic_satisfied_ids:
                verdicts.append(verdict)
            else:
                verdicts.append(
                    CriterionVerdict(
                        criterion_id=verdict.criterion_id,
                        state="unsatisfied",
                        reason_code="no_evidence",
                    )
                )
            continue
        verdicts.append(verdict)
    return verdicts


def _run_criteria_for_verdicts(
    run_criteria: list[CompletionCriterion], *verdict_groups: list[CriterionVerdict]
) -> list[CompletionCriterion]:
    # Drops verdict-less value-agnostic fallback criteria in the structured-record fast path.
    verdict_ids = {verdict.criterion_id for verdicts in verdict_groups for verdict in verdicts}
    return [criterion for criterion in run_criteria if criterion.id in verdict_ids]


def _deterministic_run_verification_result(
    run_criteria: list[CompletionCriterion],
    snapshot: RunEvidenceSnapshot,
    *,
    carrier_verdicts: tuple[CriterionVerdict, ...] = (),
) -> tuple[CompletionVerificationResult | None, list[CompletionCriterion]]:
    """Return a deterministic run-plane verdict when the typed graders cover it.

    Provider-record and present-value graders are exact enough to bypass the LLM
    judge only when they satisfy every run-plane criterion. Contradictions also
    bypass the judge, because the output is already semantically invalid. Any
    remaining criterion stays fail-closed through the normal judge path.
    """
    criterion_ids = [criterion.id for criterion in run_criteria]
    contingent_ids, contingent_on_by_id, contingent_path_by_id = _contingent_metadata_for_criteria(run_criteria)
    structural_unfired_ids = structural_unfired_contingent_criterion_ids(run_criteria, snapshot)
    semantic_verdicts = grade_record_semantic_consistency(run_criteria, snapshot)
    if any(not verdict.satisfied for verdict in semantic_verdicts):
        return (
            CompletionVerificationResult(
                status="evaluated",
                criterion_ids=criterion_ids,
                verdicts=list(semantic_verdicts),
                contingent_criterion_ids=contingent_ids,
                contingent_on_by_criterion_id=contingent_on_by_id,
                contingent_antecedent_output_path_by_criterion_id=contingent_path_by_id,
                structural_unfired_criterion_ids=structural_unfired_ids,
            ),
            [],
        )

    deterministic_by_id: dict[str, CriterionVerdict] = {}
    for verdict in grade_present_value_criteria(run_criteria, snapshot):
        deterministic_by_id[verdict.criterion_id] = verdict
    for verdict in grade_structured_record_criteria(run_criteria, snapshot):
        deterministic_by_id[verdict.criterion_id] = verdict
    for verdict in grade_fallback_floor_reached_end_state_criteria(
        run_criteria, snapshot, carrier_verdicts=carrier_verdicts
    ):
        deterministic_by_id[verdict.criterion_id] = verdict
    for verdict in grade_terminal_goal_record_criteria(run_criteria, snapshot):
        deterministic_by_id[verdict.criterion_id] = verdict
    for verdict in grade_registered_download_criteria(run_criteria, snapshot):
        deterministic_by_id[verdict.criterion_id] = verdict
    for verdict in grade_validation_classification_criteria(run_criteria, snapshot):
        deterministic_by_id[verdict.criterion_id] = verdict
    for verdict in semantic_verdicts:
        deterministic_by_id[verdict.criterion_id] = verdict

    deterministic_verdicts = [deterministic_by_id.get(criterion_id) for criterion_id in criterion_ids]
    if criterion_ids and all(verdict is not None and verdict.satisfied for verdict in deterministic_verdicts):
        return (
            CompletionVerificationResult(
                status="evaluated",
                criterion_ids=criterion_ids,
                verdicts=[deterministic_by_id[criterion_id] for criterion_id in criterion_ids],
                contingent_criterion_ids=contingent_ids,
                contingent_on_by_criterion_id=contingent_on_by_id,
                contingent_antecedent_output_path_by_criterion_id=contingent_path_by_id,
                structural_unfired_criterion_ids=structural_unfired_ids,
            ),
            [],
        )

    remaining_criteria = [criterion for criterion in run_criteria if criterion.id not in deterministic_by_id]
    if not deterministic_by_id:
        return None, remaining_criteria
    return (
        CompletionVerificationResult(
            status="evaluated",
            criterion_ids=criterion_ids,
            verdicts=list(deterministic_by_id.values()),
            contingent_criterion_ids=contingent_ids,
            contingent_on_by_criterion_id=contingent_on_by_id,
            contingent_antecedent_output_path_by_criterion_id=contingent_path_by_id,
            structural_unfired_criterion_ids=structural_unfired_ids,
        ),
        remaining_criteria,
    )


async def _maybe_run_completion_verification(
    copilot_ctx: Any, result: dict[str, Any], handler_start: float
) -> CompletionVerificationResult | None:
    if getattr(copilot_ctx, "copilot_total_timeout_exceeded", False):
        return None
    if not (
        _is_outcome_evidence_candidate(copilot_ctx, result)
        or _is_unfinished_run_verification_candidate(copilot_ctx, result)
    ):
        return None
    criteria = _completion_verification_criteria(copilot_ctx)
    criteria = _reconcile_download_completion_criterion(copilot_ctx, result, criteria)
    verification = await _completion_verification_from_run_result(copilot_ctx, result, handler_start, criteria)
    return carry_degraded_criterion_ids(verification, criteria) if verification is not None else None


async def _completion_verification_from_run_result(
    copilot_ctx: Any, result: dict[str, Any], handler_start: float, criteria: list[CompletionCriterion]
) -> CompletionVerificationResult | None:
    run_criteria, definition_criteria = _split_criteria_by_plane(criteria)
    criterion_ids = [criterion.id for criterion in criteria]
    contingent_ids, contingent_on_by_id, contingent_path_by_id = _contingent_metadata_for_criteria(criteria)
    run_contingent_ids, run_contingent_on_by_id, run_contingent_path_by_id = _contingent_metadata_for_criteria(
        run_criteria
    )
    if _classifier_status(copilot_ctx) == "fallback" and not run_criteria:
        return _no_gradeable_run_plane_result(criterion_ids)
    if not criteria:
        return None
    definition_verdicts = (
        grade_definition_criteria(definition_criteria, _definition_plane_workflow_yaml(copilot_ctx))
        if definition_criteria
        else []
    )
    if not run_criteria:
        return combine_verification_results(
            criterion_ids,
            None,
            definition_verdicts,
            contingent_criterion_ids=contingent_ids,
            contingent_on_by_criterion_id=contingent_on_by_id,
            contingent_antecedent_output_path_by_criterion_id=contingent_path_by_id,
        )
    snapshot = _build_run_evidence_snapshot(copilot_ctx, result)
    run_structural_unfired_ids = structural_unfired_contingent_criterion_ids(run_criteria, snapshot)
    requested_output_criteria, judgeable_run_criteria = split_requested_output_criteria(run_criteria)
    requested_output_verdicts = (
        grade_requested_output_criteria(copilot_ctx, requested_output_criteria, snapshot)
        if requested_output_criteria
        else []
    )
    carrier_verdicts = _carrier_floor_verdicts(requested_output_verdicts)
    if judgeable_run_criteria and all(
        criterion.id in _STRUCTURED_RECORD_CRITERION_IDS for criterion in judgeable_run_criteria
    ):
        # Classifier-fallback criteria are value-agnostic (graded on record shape, not the
        # requested entity) and the judge cannot disambiguate them either, so a well-shaped record
        # for the wrong entity must not read as verified. Treat the run plane as criteria-less and
        # surface only a structural contradiction as a suspicious-success signal.
        contradictions = [
            verdict
            for verdict in grade_structured_record_criteria(judgeable_run_criteria, snapshot)
            if not verdict.satisfied
        ]
        if contradictions or requested_output_verdicts:
            scoped_run_criteria = _run_criteria_for_verdicts(run_criteria, requested_output_verdicts, contradictions)
            return combine_verification_results(
                [criterion.id for criterion in scoped_run_criteria]
                + [criterion.id for criterion in definition_criteria],
                _merge_run_verdicts(
                    scoped_run_criteria,
                    requested_output_verdicts,
                    contradictions,
                    structural_unfired_criterion_ids=run_structural_unfired_ids,
                ),
                definition_verdicts,
                contingent_criterion_ids=contingent_ids,
                contingent_on_by_criterion_id=contingent_on_by_id,
                contingent_antecedent_output_path_by_criterion_id=contingent_path_by_id,
            )
        if not definition_verdicts:
            return None
        definition_contingent_ids, definition_contingent_on_by_id, definition_contingent_path_by_id = (
            _contingent_metadata_for_criteria(definition_criteria)
        )
        return combine_verification_results(
            [criterion.id for criterion in definition_criteria],
            None,
            definition_verdicts,
            contingent_criterion_ids=definition_contingent_ids,
            contingent_on_by_criterion_id=definition_contingent_on_by_id,
            contingent_antecedent_output_path_by_criterion_id=definition_contingent_path_by_id,
        )
    if not snapshot.has_evidence():
        run_result = _merge_run_verdicts(
            run_criteria,
            requested_output_verdicts,
            [
                CriterionVerdict(criterion_id=criterion.id, state="unsatisfied", reason_code="no_evidence")
                for criterion in judgeable_run_criteria
            ],
            contingent_criterion_ids=run_contingent_ids,
            contingent_on_by_criterion_id=run_contingent_on_by_id,
            contingent_antecedent_output_path_by_criterion_id=run_contingent_path_by_id,
            structural_unfired_criterion_ids=run_structural_unfired_ids,
        )
    elif not judgeable_run_criteria:
        run_result = _merge_run_verdicts(
            run_criteria,
            requested_output_verdicts,
            grade_terminal_goal_record_corroboration(snapshot),
            structural_unfired_criterion_ids=run_structural_unfired_ids,
        )
    else:
        deterministic_result, remaining_criteria = _deterministic_run_verification_result(
            judgeable_run_criteria, snapshot, carrier_verdicts=carrier_verdicts
        )
        if deterministic_result is not None and not remaining_criteria:
            run_result = _merge_run_verdicts(
                run_criteria,
                requested_output_verdicts,
                deterministic_result.verdicts,
                structural_unfired_criterion_ids=run_structural_unfired_ids,
            )
        else:
            handler = await _completion_verification_handler(copilot_ctx)
            if handler is None:
                requested_output_result = _merge_run_verdicts_if_requested_output_exists(
                    run_criteria,
                    requested_output_verdicts,
                    snapshot=snapshot,
                    structural_unfired_criterion_ids=run_structural_unfired_ids,
                )
                if requested_output_result is not None:
                    return combine_verification_results(
                        criterion_ids,
                        requested_output_result,
                        definition_verdicts,
                        contingent_criterion_ids=contingent_ids,
                        contingent_on_by_criterion_id=contingent_on_by_id,
                        contingent_antecedent_output_path_by_criterion_id=contingent_path_by_id,
                    )
                return CompletionVerificationResult(
                    status="unavailable",
                    criterion_ids=criterion_ids,
                    contingent_criterion_ids=contingent_ids,
                    contingent_on_by_criterion_id=contingent_on_by_id,
                    contingent_antecedent_output_path_by_criterion_id=contingent_path_by_id,
                    structural_unfired_criterion_ids=run_structural_unfired_ids,
                )
            # Too little budget to verify a candidate run: fail closed (unavailable)
            # rather than let the run-status proxy claim an unverified outcome as success.
            remaining = RUN_BLOCKS_SAFETY_CEILING_SECONDS - (time.monotonic() - handler_start)
            if (
                remaining
                <= settings.COPILOT_COMPLETION_JUDGE_TIMEOUT_SECONDS + _COMPLETION_VERIFICATION_BUDGET_MARGIN_SECONDS
            ):
                requested_output_result = _merge_run_verdicts_if_requested_output_exists(
                    run_criteria,
                    requested_output_verdicts,
                    snapshot=snapshot,
                    structural_unfired_criterion_ids=run_structural_unfired_ids,
                )
                if requested_output_result is not None:
                    return combine_verification_results(
                        criterion_ids,
                        requested_output_result,
                        definition_verdicts,
                        contingent_criterion_ids=contingent_ids,
                        contingent_on_by_criterion_id=contingent_on_by_id,
                        contingent_antecedent_output_path_by_criterion_id=contingent_path_by_id,
                    )
                return CompletionVerificationResult(
                    status="unavailable",
                    criterion_ids=criterion_ids,
                    contingent_criterion_ids=contingent_ids,
                    contingent_on_by_criterion_id=contingent_on_by_id,
                    contingent_antecedent_output_path_by_criterion_id=contingent_path_by_id,
                    structural_unfired_criterion_ids=run_structural_unfired_ids,
                )
            judged_result = await evaluate_completion_criteria(remaining_criteria, snapshot, handler)
            if judged_result.status != "evaluated":
                # Deterministic requested-output verdicts can still ground completion when the judge abstains.
                run_result = (
                    _merge_run_verdicts_if_requested_output_exists(
                        run_criteria,
                        requested_output_verdicts,
                        snapshot=snapshot,
                        structural_unfired_criterion_ids=run_structural_unfired_ids,
                    )
                    or judged_result
                )
            else:
                verdicts = list(requested_output_verdicts)
                if deterministic_result is not None:
                    verdicts.extend(deterministic_result.verdicts)
                verdicts.extend(
                    _filter_judged_fallback_floor_satisfaction(
                        judgeable_run_criteria,
                        judged_result,
                        deterministic_result,
                    )
                )
                run_result = CompletionVerificationResult(
                    status="evaluated",
                    criterion_ids=[criterion.id for criterion in run_criteria],
                    verdicts=verdicts,
                    contingent_criterion_ids=run_contingent_ids,
                    contingent_on_by_criterion_id=run_contingent_on_by_id,
                    contingent_antecedent_output_path_by_criterion_id=run_contingent_path_by_id,
                    structural_unfired_criterion_ids=run_structural_unfired_ids,
                )
                run_result = _apply_present_value_upgrades(
                    run_result,
                    judgeable_run_criteria,
                    snapshot,
                    include_terminal_goal_records=True,
                    carrier_verdicts=carrier_verdicts,
                )
                run_result = _merge_run_verdicts(
                    run_criteria,
                    requested_output_verdicts,
                    run_result.verdicts,
                    structural_unfired_criterion_ids=run_structural_unfired_ids,
                )
    return combine_verification_results(
        criterion_ids,
        run_result,
        definition_verdicts,
        contingent_criterion_ids=contingent_ids,
        contingent_on_by_criterion_id=contingent_on_by_id,
        contingent_antecedent_output_path_by_criterion_id=contingent_path_by_id,
    )


def _outcome_unverified_reason(
    copilot_ctx: Any, completion_verification: CompletionVerificationResult | None
) -> str | None:
    if completion_verification is None:
        return None
    if completion_verification.status == "evaluated":
        if completion_verification.is_fully_satisfied():
            return None
        if completion_verification.no_gradeable_run_plane:
            return (
                "The run completed but the goal outcome could not be independently verified "
                "(no run-gradeable outcome in this contract). Review the draft before using it."
            )
        policy = getattr(copilot_ctx, "request_policy", None)
        criteria: list[CompletionCriterion] = list(policy.completion_criteria) if policy is not None else []
        known_good = _known_good_revision_hint(copilot_ctx)
        missing_detail = _missing_evidence_detail(completion_verification, criteria)
        validation_review_hint = _validation_review_output_contract_hint(completion_verification, criteria)
        if missing_detail:
            return (
                "The run completed but did not demonstrate the goal outcome(s). "
                f"Missing evidence: {missing_detail}. "
                "Add or fix the block that produces the missing outcome evidence, then "
                f"re-run.{validation_review_hint}{known_good}"
            )
        unmet = summarize_unsatisfied_outcomes(completion_verification, criteria)
        detail = f": {unmet}" if unmet else ""
        # Keep the legacy fallback live for malformed evaluated results with no unmet verdict details.
        return (
            f"The run completed but did not demonstrate the goal outcome(s){detail}. "
            "Add or fix the block that produces the missing outcome evidence, "
            f"then re-run.{validation_review_hint}{known_good}"
        )
    # An 'unavailable' result reaches here only when verification was required;
    # fail closed and ask for a re-run rather than claim an unverified success.
    return (
        "The run completed but the goal outcome could not be verified (verification was unavailable). "
        "Re-run to verify the outcome before reporting success."
    )


def _known_good_revision_hint(copilot_ctx: Any) -> str:
    turn_state = getattr(copilot_ctx, "completion_criteria_turn_state", None)
    if turn_state is not None and getattr(turn_state, "known_good_yaml_available", False):
        return (
            " A previously tested revision of this workflow satisfied every criterion; if repairs regress "
            "working blocks, prefer restoring that revision over rewriting them."
        )
    return ""


def _validation_review_output_contract_hint(
    completion_verification: CompletionVerificationResult, criteria: list[CompletionCriterion]
) -> str:
    fallback_floor_ids = {criterion.id for criterion in criteria if is_fallback_floor_base_criterion(criterion)}
    if not fallback_floor_ids:
        return ""
    for verdict in completion_verification.verdicts:
        if verdict.criterion_id in fallback_floor_ids and verdict.state == "unsatisfied":
            if verdict.reason_code == "no_evidence":
                return _VALIDATION_REVIEW_OUTPUT_CONTRACT_HINT
    return ""


def _missing_evidence_detail(
    completion_verification: CompletionVerificationResult, criteria: list[CompletionCriterion]
) -> str | None:
    outcome_by_id = {criterion.id: criterion.outcome for criterion in criteria}
    parts: list[str] = []
    for verdict in completion_verification.verdicts:
        if verdict.satisfied or completion_verification.is_structural_contingent_abstention(verdict):
            continue
        missing_evidence = verdict_missing_evidence(verdict)
        if not missing_evidence:
            continue
        outcome = outcome_by_id.get(verdict.criterion_id)
        if isinstance(outcome, str) and outcome.strip():
            parts.append(f"{outcome}: {missing_evidence}")
        else:
            parts.append(f"{verdict.criterion_id}: {missing_evidence}")
    return "; ".join(parts) if parts else None


def _outcome_failure_warrants_repair(
    copilot_ctx: Any, completion_verification: CompletionVerificationResult | None
) -> bool:
    """Whether an unmet outcome should route to suspicious-success/repair rather
    than continue-building.

    Contradicting evidence is always a real failure. Absence of evidence is a
    failure only once the workflow has an outcome-producing block; while the agent
    is still adding blocks toward the goal, an unmet criterion is expected, and the
    run should keep building. Terminal success stays withheld in both cases via the
    verification result, so this only governs the repair directive, not the gate.
    """
    if completion_verification is None:
        return False
    if only_degraded_blocking(completion_verification):
        return False
    if any(verdict.reason_code == "evidence_contradicts" for verdict in completion_verification.verdicts):
        return True
    # Repair needs at least one affirmatively unsatisfied criterion; unknown alone
    # (absent judge signal, unmappable definition checks) never warrants repair.
    if not any(
        verdict.state == "unsatisfied" and not _is_structural_requested_output_abstention(verdict)
        for verdict in completion_verification.verdicts
    ):
        return False
    return _current_workflow_has_evidence_block(copilot_ctx)


def _tool_visible_result_after_completion_verification(
    copilot_ctx: Any,
    result: dict[str, Any],
    completion_verification: CompletionVerificationResult | None,
) -> dict[str, Any]:
    if outcome_fully_verified(copilot_ctx):
        return result
    outcome_unverified_reason = _outcome_unverified_reason(copilot_ctx, completion_verification)
    if outcome_unverified_reason is None:
        return result
    if not _outcome_failure_warrants_repair(copilot_ctx, completion_verification):
        return result

    data = result.get("data")
    copied_data = dict(data) if isinstance(data, dict) else {}
    copied_data["failure_reason"] = outcome_unverified_reason
    copied_data["completion_verification"] = (
        completion_verification.to_trace_data() if completion_verification is not None else None
    )
    categories = copied_data.get("failure_categories")
    copied_categories = list(categories) if isinstance(categories, list) else []
    copied_categories.insert(
        0,
        {
            "category": "OUTCOME_UNVERIFIED",
            "confidence_float": 1.0,
            "reasoning": outcome_unverified_reason,
        },
    )
    copied_data["failure_categories"] = copied_categories
    return {
        **result,
        "ok": False,
        "error": outcome_unverified_reason,
        "data": copied_data,
    }


def _emit_completion_verification_trace(
    copilot_ctx: Any, completion_verification: CompletionVerificationResult
) -> None:
    block_count = getattr(copilot_ctx, "last_update_block_count", None)
    policy = getattr(copilot_ctx, "request_policy", None)
    contract = policy.completion_contract if policy is not None else None
    heuristic_would_block = isinstance(block_count, int) and _goal_likely_needs_more_blocks(
        getattr(copilot_ctx, "user_message", ""), block_count, contract
    )
    trace_data = {
        **completion_verification.to_trace_data(),
        "heuristic_would_block": heuristic_would_block,
        "evidence_block_present": _current_workflow_has_evidence_block(copilot_ctx),
        "warrants_repair": _outcome_failure_warrants_repair(copilot_ctx, completion_verification),
    }
    LOG.info(
        "copilot completion verification",
        **{f"completion_verification_{key}": value for key, value in trace_data.items()},
    )
    with copilot_span("completion_verification", data=trace_data):
        pass
