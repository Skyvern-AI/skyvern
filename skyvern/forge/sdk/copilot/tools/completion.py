import time
from typing import Any

import structlog

from skyvern.config import settings
from skyvern.forge.sdk.copilot.completion_criteria_store import note_adjudication_on_turn_state
from skyvern.forge.sdk.copilot.completion_verification import (
    _STRUCTURED_RECORD_CRITERION_IDS,
    CompletionVerificationResult,
    CriterionVerdict,
    RunEvidenceSnapshot,
    combine_verification_results,
    evaluate_completion_criteria,
    grade_definition_criteria,
    grade_present_value_criteria,
    grade_record_semantic_consistency,
    grade_structured_record_criteria,
    summarize_unsatisfied_outcomes,
    verdict_missing_evidence,
)
from skyvern.forge.sdk.copilot.enforcement import _goal_likely_needs_more_blocks
from skyvern.forge.sdk.copilot.llm_config import resolve_main_copilot_handler
from skyvern.forge.sdk.copilot.outcome_verification_trace import record_completion_verification
from skyvern.forge.sdk.copilot.output_utils import iter_failure_reasons
from skyvern.forge.sdk.copilot.reached_download_target import REGISTERED_DOWNLOAD_OUTPUT_KEYS
from skyvern.forge.sdk.copilot.request_policy import CompletionCriterion
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


def _completion_verification_criteria(copilot_ctx: Any) -> list[Any]:
    policy = getattr(copilot_ctx, "request_policy", None)
    # A method-mandated criterion asserts HOW the goal was reached; the outcome
    # judge sees only end-state evidence.
    return [c for c in (policy.completion_criteria if policy is not None else []) if not c.method_mandated]


def _split_criteria_by_plane(criteria: list[Any]) -> tuple[list[CompletionCriterion], list[CompletionCriterion]]:
    run_criteria = [c for c in criteria if getattr(c, "level", "run") != "definition"]
    definition_criteria = [c for c in criteria if getattr(c, "level", "run") == "definition"]
    return run_criteria, definition_criteria


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
    if isinstance(observed_data, dict) and observed_data:
        block_outputs["current_page_observation"] = observed_data
    elif observed_data is not None:
        block_outputs["current_page_observation"] = str(observed_data)
    return RunEvidenceSnapshot(
        workflow_run_id=run_id if isinstance(run_id, str) else None,
        block_outputs=block_outputs,
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
    if not criteria:
        return None
    run_criteria, definition_criteria = _split_criteria_by_plane(criteria)
    criterion_ids = [criterion.id for criterion in criteria]
    definition_verdicts = (
        grade_definition_criteria(definition_criteria, _definition_plane_workflow_yaml(copilot_ctx))
        if definition_criteria
        else []
    )
    if not run_criteria:
        verification = combine_verification_results(criterion_ids, None, definition_verdicts)
    else:
        handler = await _completion_verification_handler(copilot_ctx)
        if handler is None:
            return None
        remaining = _copilot_seconds_remaining(copilot_ctx)
        if (
            remaining is not None
            and remaining
            <= settings.COPILOT_COMPLETION_JUDGE_TIMEOUT_SECONDS + _COMPLETION_VERIFICATION_BUDGET_MARGIN_SECONDS
        ):
            verification = CompletionVerificationResult(status="unavailable")
        else:
            snapshot = _build_page_observation_evidence_snapshot(
                copilot_ctx,
                url=url,
                title=title,
                observed_data=observed_data,
            )
            if not snapshot.has_evidence():
                run_result = CompletionVerificationResult(
                    status="evaluated",
                    criterion_ids=[criterion.id for criterion in run_criteria],
                    verdicts=[
                        CriterionVerdict(criterion_id=criterion.id, state="unsatisfied", reason_code="no_evidence")
                        for criterion in run_criteria
                    ],
                )
            else:
                run_result = await evaluate_completion_criteria(run_criteria, snapshot, handler)
                run_result = _apply_present_value_upgrades(run_result, run_criteria, snapshot)
            verification = combine_verification_results(criterion_ids, run_result, definition_verdicts)

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
    if not isinstance(output, dict) or not any(output.get(key) for key in REGISTERED_DOWNLOAD_OUTPUT_KEYS):
        return output
    names: list[str] = []
    if name := _download_file_name(output.get("downloaded_file_name")):
        names.append(name)
    files = output.get("downloaded_files")
    if isinstance(files, list):
        names.extend(name for item in files[:_MAX_EVIDENCE_FILE_NAMES] if (name := _download_file_name(item)))
    urls = output.get("downloaded_file_urls")
    if isinstance(urls, list):
        names.extend(name for item in urls[:_MAX_EVIDENCE_FILE_NAMES] if (name := _download_file_name(item)))
    payload: dict[str, Any] = {"download_registered": True}
    if isinstance(files, list):
        payload["downloaded_file_count"] = len(files)
    if isinstance(urls, list):
        payload["downloaded_file_url_count"] = len(urls)
    artifacts = output.get("downloaded_file_artifact_ids")
    if isinstance(artifacts, list):
        payload["downloaded_file_artifact_count"] = len(artifacts)
    if names:
        payload["downloaded_file_names"] = list(dict.fromkeys(names))[:_MAX_EVIDENCE_FILE_NAMES]
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
    if isinstance(blocks, list):
        for block in blocks:
            if not isinstance(block, dict):
                continue
            label = block.get("label")
            output = block.get("extracted_data")
            evidence_output = _completion_evidence_payload(output)
            if isinstance(label, str) and label in current_labels and _is_meaningful_extracted_data(evidence_output):
                block_outputs[label] = evidence_output
            for output_key, output_value in _workflow_output_parameter_payloads(output).items():
                if _is_meaningful_extracted_data(output_value):
                    block_outputs[output_key] = output_value
    for output_key, output_value in _workflow_output_parameter_payloads(data.get("output")).items():
        if _is_meaningful_extracted_data(output_value):
            block_outputs[output_key] = output_value
    for registered in _registered_output_parameter_payloads(data):
        registered_output_key = registered.get("output_parameter_key")
        registered_output_value = registered.get("value")
        registered_block_label = registered.get("block_label")
        if not _is_meaningful_extracted_data(registered_output_value):
            continue
        if isinstance(registered_output_key, str) and registered_output_key:
            block_outputs[registered_output_key] = registered_output_value
        if isinstance(registered_block_label, str) and registered_block_label in current_labels:
            if isinstance(registered_output_key, str) and registered_output_key:
                existing = block_outputs.get(registered_block_label)
                if isinstance(existing, dict):
                    existing.setdefault(registered_output_key, registered_output_value)
                else:
                    block_outputs[registered_block_label] = {registered_output_key: registered_output_value}
            else:
                block_outputs[registered_block_label] = registered_output_value
    executed = data.get("executed_block_labels")
    executed_block_labels = [str(label) for label in executed] if isinstance(executed, list) else []
    page_title = data.get("page_title")
    run_id = data.get("workflow_run_id")
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
    )


def _apply_present_value_upgrades(
    run_result: CompletionVerificationResult,
    run_criteria: list[CompletionCriterion],
    snapshot: RunEvidenceSnapshot,
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
        status="evaluated", criterion_ids=list(run_result.criterion_ids), verdicts=verdicts
    )


def _deterministic_run_verification_result(
    run_criteria: list[CompletionCriterion],
    snapshot: RunEvidenceSnapshot,
) -> tuple[CompletionVerificationResult | None, list[CompletionCriterion]]:
    """Return a deterministic run-plane verdict when the typed graders cover it.

    Provider-record and present-value graders are exact enough to bypass the LLM
    judge only when they satisfy every run-plane criterion. Contradictions also
    bypass the judge, because the output is already semantically invalid. Any
    remaining criterion stays fail-closed through the normal judge path.
    """
    criterion_ids = [criterion.id for criterion in run_criteria]
    semantic_verdicts = grade_record_semantic_consistency(run_criteria, snapshot)
    if any(not verdict.satisfied for verdict in semantic_verdicts):
        return (
            CompletionVerificationResult(
                status="evaluated",
                criterion_ids=criterion_ids,
                verdicts=list(semantic_verdicts),
            ),
            [],
        )

    deterministic_by_id: dict[str, CriterionVerdict] = {}
    for verdict in grade_present_value_criteria(run_criteria, snapshot):
        deterministic_by_id[verdict.criterion_id] = verdict
    for verdict in grade_structured_record_criteria(run_criteria, snapshot):
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
        ),
        remaining_criteria,
    )


async def _maybe_run_completion_verification(
    copilot_ctx: Any, result: dict[str, Any], handler_start: float
) -> CompletionVerificationResult | None:
    if getattr(copilot_ctx, "copilot_total_timeout_exceeded", False):
        return None
    criteria = _completion_verification_criteria(copilot_ctx)
    if not criteria:
        return None
    if not (
        _is_outcome_evidence_candidate(copilot_ctx, result)
        or _is_unfinished_run_verification_candidate(copilot_ctx, result)
    ):
        return None
    run_criteria, definition_criteria = _split_criteria_by_plane(criteria)
    criterion_ids = [criterion.id for criterion in criteria]
    definition_verdicts = (
        grade_definition_criteria(definition_criteria, _definition_plane_workflow_yaml(copilot_ctx))
        if definition_criteria
        else []
    )
    if run_criteria and all(criterion.id in _STRUCTURED_RECORD_CRITERION_IDS for criterion in run_criteria):
        # Classifier-fallback criteria are value-agnostic (graded on record shape, not the
        # requested entity) and the judge cannot disambiguate them either, so a well-shaped record
        # for the wrong entity must not read as verified. Treat the run plane as criteria-less and
        # surface only a structural contradiction as a suspicious-success signal.
        snapshot = _build_run_evidence_snapshot(copilot_ctx, result)
        contradictions = [
            verdict for verdict in grade_structured_record_criteria(run_criteria, snapshot) if not verdict.satisfied
        ]
        if contradictions:
            return combine_verification_results(
                criterion_ids,
                CompletionVerificationResult(
                    status="evaluated",
                    criterion_ids=[criterion.id for criterion in run_criteria],
                    verdicts=contradictions,
                ),
                definition_verdicts,
            )
        if not definition_verdicts:
            return None
        return combine_verification_results(
            [criterion.id for criterion in definition_criteria], None, definition_verdicts
        )
    if not run_criteria:
        return combine_verification_results(criterion_ids, None, definition_verdicts)
    snapshot = _build_run_evidence_snapshot(copilot_ctx, result)
    if not snapshot.has_evidence():
        run_result = CompletionVerificationResult(
            status="evaluated",
            criterion_ids=[criterion.id for criterion in run_criteria],
            verdicts=[
                CriterionVerdict(criterion_id=criterion.id, state="unsatisfied", reason_code="no_evidence")
                for criterion in run_criteria
            ],
        )
    else:
        handler = await _completion_verification_handler(copilot_ctx)
        if handler is None:
            return CompletionVerificationResult(status="unavailable")
        # Too little budget to verify a candidate run: fail closed (unavailable)
        # rather than let the run-status proxy claim an unverified outcome as success.
        remaining = RUN_BLOCKS_SAFETY_CEILING_SECONDS - (time.monotonic() - handler_start)
        if (
            remaining
            <= settings.COPILOT_COMPLETION_JUDGE_TIMEOUT_SECONDS + _COMPLETION_VERIFICATION_BUDGET_MARGIN_SECONDS
        ):
            return CompletionVerificationResult(status="unavailable")
        deterministic_result, remaining_criteria = _deterministic_run_verification_result(run_criteria, snapshot)
        if deterministic_result is not None and not remaining_criteria:
            run_result = deterministic_result
        else:
            judged_result = await evaluate_completion_criteria(remaining_criteria, snapshot, handler)
            if judged_result.status != "evaluated":
                run_result = judged_result
            else:
                verdicts = []
                if deterministic_result is not None:
                    verdicts.extend(deterministic_result.verdicts)
                verdicts.extend(judged_result.verdicts)
                run_result = CompletionVerificationResult(
                    status="evaluated",
                    criterion_ids=[criterion.id for criterion in run_criteria],
                    verdicts=verdicts,
                )
                run_result = _apply_present_value_upgrades(run_result, run_criteria, snapshot)
    return combine_verification_results(criterion_ids, run_result, definition_verdicts)


def _outcome_unverified_reason(
    copilot_ctx: Any, completion_verification: CompletionVerificationResult | None
) -> str | None:
    if completion_verification is None:
        return None
    if completion_verification.status == "evaluated":
        if completion_verification.is_fully_satisfied():
            return None
        policy = getattr(copilot_ctx, "request_policy", None)
        criteria: list[CompletionCriterion] = list(policy.completion_criteria) if policy is not None else []
        known_good = _known_good_revision_hint(copilot_ctx)
        missing_detail = _missing_evidence_detail(completion_verification, criteria)
        if missing_detail:
            return (
                "The run completed but did not demonstrate the goal outcome(s). "
                f"Missing evidence: {missing_detail}. "
                f"Add or fix the block that produces the missing outcome evidence, then re-run.{known_good}"
            )
        unmet = summarize_unsatisfied_outcomes(completion_verification, criteria)
        detail = f": {unmet}" if unmet else ""
        # Keep the legacy fallback live for malformed evaluated results with no unmet verdict details.
        return (
            f"The run completed but did not demonstrate the goal outcome(s){detail}. "
            "Add or fix the block that produces the missing outcome evidence, "
            f"then re-run.{known_good}"
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


def _missing_evidence_detail(
    completion_verification: CompletionVerificationResult, criteria: list[CompletionCriterion]
) -> str | None:
    outcome_by_id = {criterion.id: criterion.outcome for criterion in criteria}
    parts: list[str] = []
    for verdict in completion_verification.verdicts:
        if verdict.satisfied:
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
    if any(verdict.reason_code == "evidence_contradicts" for verdict in completion_verification.verdicts):
        return True
    # Repair needs at least one affirmatively unsatisfied criterion; unknown alone
    # (absent judge signal, unmappable definition checks) never warrants repair.
    if not any(verdict.state == "unsatisfied" for verdict in completion_verification.verdicts):
        return False
    return _current_workflow_has_evidence_block(copilot_ctx)


def _tool_visible_result_after_completion_verification(
    copilot_ctx: Any,
    result: dict[str, Any],
    completion_verification: CompletionVerificationResult | None,
) -> dict[str, Any]:
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
