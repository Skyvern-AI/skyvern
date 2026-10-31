"""A failed run's final anti-bot diagnostic: the run HAR's class-level evidence, the run's challenge-solve receipts and
the failure facts persisted when the run failed, read together once cleanup has the closed recording. The diagnostic
is pure code: no model is asked for it.

The result is an output-only failure_category entry (HAR_ANTI_BOT_DIAGNOSTIC_REASON_CODE) carrying the anti-bot system
errors qualified evidence supports. A standalone task also appends those to its errors[]; a workflow run's response
projects them from the entry. Status, retry, credential fallback and attribution have already read the run before
this runs.

Separately, a run with a user error_code_mapping may ask the existing user-error prompt once whether the recording's
ordinary network facts fit one of the user's codes. That one request is claimed in failure_category before it is
sent, so no cleanup repeats it whatever its outcome. Its codes are report-only: status, retry and the attempt's
recorded error codes were decided before it ran."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Collection
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

import structlog

from skyvern.constants import ERROR_CODE_REASONING_MAX_LENGTH
from skyvern.errors.errors import (
    ANTI_BOT_SYSTEM_ERROR_CODES,
    ErrorType,
    UserDefinedError,
    anti_bot_system_errors,
    filter_to_user_defined_codes,
)
from skyvern.forge import app
from skyvern.forge.antibot_evidence import run_solve_receipts, unresolved_challenges
from skyvern.forge.failure_classifier import (
    DIAGNOSABLE_FAILURE_STATUSES,
    HAR_ANTI_BOT_DIAGNOSTIC_REASON_CODE,
    FailureCategory,
    final_har_user_mapping_digest,
)
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.api.llm.api_handler_factory import get_org_aware_secondary_llm_api_handler
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.schemas.har_evidence import (
    HarEvidence,
    HarEvidenceAvailability,
    HarEvidenceEvent,
    HarEvidenceVisit,
    is_final_har_user_match_entry,
)
from skyvern.forge.sdk.schemas.task_v2 import TaskV2
from skyvern.forge.sdk.schemas.tasks import Task
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRun, WorkflowRunStatus
from skyvern.utils.secret_redaction import redact_secrets_from_text
from skyvern.webeye.actions.actions import ActionStatus

LOG = structlog.get_logger(__name__)

# Requests recorded just after the failure write still belong to the failing attempt.
_LATE_GRACE = timedelta(seconds=5)
# The HAR names no action, frame or initiator, so nothing recorded ties an enforcement event to the operation that
# failed: whether it caused the failure stays unknown, however close in time it was.
CONTRIBUTION_UNKNOWN = "unknown"
_USER_MATCH_TIMEOUT_S = 15
_USER_MATCH_PROMPT = "surface-user-defined-errors"


class Enforcement(StrEnum):
    explicit_block = "explicit_block"
    repeat_block = "repeat_block"
    unresolved_challenge = "unresolved_challenge"
    challenge_resource_failed = "challenge_resource_failed"
    none_observed = "none_observed"


@dataclass(frozen=True)
class FailedActionFacts:
    action_id: str | None
    step_id: str | None
    screenshot_artifact_id: str | None


@dataclass(frozen=True)
class FailureContext:
    """What the failing task persisted when it failed, read back rather than re-captured from a live page."""

    task_id: str | None
    attempt_number: int | None
    window_start: datetime | None
    finished_at: datetime | None
    failed_action: FailedActionFacts | None
    # The runs whose challenge-solve receipts belong to this failure.
    run_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class FinalDiagnostic:
    availability: HarEvidenceAvailability
    vendors_observed: tuple[str, ...] = ()
    enforcement: Enforcement = Enforcement.none_observed
    enforcing_vendor: str | None = None
    challenge_type: str | None = None
    http_status: int | None = None
    url_class: str | None = None
    evidence_refs: tuple[str, ...] = ()
    system_errors: tuple[dict[str, Any], ...] = ()


def _utc(at: datetime | None) -> datetime | None:
    if at is None:
        return None
    return at.replace(tzinfo=UTC) if at.tzinfo is None else at.astimezone(UTC)


async def freeze_failure_context(
    task: Task | None,
    *,
    organization_id: str,
    finished_at: datetime | None,
    window_start: datetime | None,
    attempt_number: int | None,
    run_ids: tuple[str, ...] = (),
) -> FailureContext:
    failed_action: FailedActionFacts | None = None
    if task is not None:
        try:
            actions = await app.DATABASE.tasks.get_task_actions(task.task_id, organization_id=organization_id)
        except Exception:
            LOG.warning("Failed to read the failing task's actions for the final diagnostic", exc_info=True)
            actions = []
        # Only an action recorded as failed is the failed action; a later successful one is not.
        action = next((a for a in reversed(actions) if a.status == ActionStatus.failed), None)
        if action is not None:
            failed_action = FailedActionFacts(
                action_id=action.action_id,
                step_id=action.step_id,
                screenshot_artifact_id=action.screenshot_artifact_id,
            )
    return FailureContext(
        task_id=task.task_id if task is not None else None,
        attempt_number=attempt_number,
        window_start=_utc(window_start),
        finished_at=_utc(finished_at),
        failed_action=failed_action,
        run_ids=run_ids,
    )


def _in_attempt(at: datetime | None, context: FailureContext) -> bool:
    if at is None:
        return False
    if context.window_start and at < context.window_start:
        return False
    return not (context.finished_at and at > context.finished_at + _LATE_GRACE)


def _attempt_visits(evidence: HarEvidence, context: FailureContext) -> list[HarEvidenceVisit]:
    """Visits with any recorded activity inside the failing attempt; the recording can span earlier attempts."""
    return [
        visit
        for visit in evidence.visits
        if any(_in_attempt(_utc(t), context) for t in (visit.started_at, visit.ended_at))
        or any(_in_attempt(_utc(e.event_at), context) for e in visit.events)
    ]


def _attempt_vendors(visits: list[HarEvidenceVisit], context: FailureContext) -> tuple[str, ...]:
    """Each visit's recorded vendors that one of its timed events names inside the attempt. A visit can span earlier
    retries and untimed entries its endpoints exclude, so its whole vendor set is not evidence about this attempt."""
    vendors: dict[str, None] = {}
    for visit in visits:
        current = {e.vendor for e in visit.events if e.vendor and _in_attempt(_utc(e.event_at), context)}
        vendors.update((vendor, None) for vendor in visit.vendors_seen if vendor in current)
    return tuple(vendors)


def _enforcement(event: HarEvidenceEvent) -> Enforcement | None:
    if event.event_type == "repeat_block_after_challenge":
        return Enforcement.repeat_block
    if event.event_type == "challenge_resource_failed":
        return Enforcement.challenge_resource_failed
    if event.event_type == "challenge_issued":
        return Enforcement.unresolved_challenge
    # A static asset refused says nothing about the page; a vendor-served 503 alone is as likely maintenance as
    # enforcement.
    if event.event_type == "explicit_block" and event.url_class in ("document", "api") and event.http_status != 503:
        return Enforcement.explicit_block
    return None


def _ref(visit: HarEvidenceVisit, index: int) -> str:
    return f"har:{visit.tab_ref}/{visit.visit_n}/{index}"


def diagnose(evidence: HarEvidence, context: FailureContext) -> FinalDiagnostic:
    """What the attempt's recording observed: vendors present and the latest enforcement. Later traffic that loads
    fine neither clears nor erases an earlier enforcement; neither is evidence about the failed operation."""
    if evidence.availability != HarEvidenceAvailability.available:
        return FinalDiagnostic(availability=evidence.availability)
    visits = _attempt_visits(evidence, context)
    vendors = _attempt_vendors(visits, context)
    latest: tuple[datetime, HarEvidenceVisit, int, HarEvidenceEvent, Enforcement] | None = None
    for visit in visits:
        for index, event in enumerate(visit.events):
            at = _utc(event.event_at)
            kind = _enforcement(event)
            if kind is None or at is None or not _in_attempt(at, context):
                continue
            if latest is None or at >= latest[0]:
                latest = (at, visit, index, event, kind)
    if latest is None:
        return FinalDiagnostic(availability=evidence.availability, vendors_observed=vendors)
    _, visit, index, event, kind = latest
    return FinalDiagnostic(
        availability=evidence.availability,
        vendors_observed=vendors,
        enforcement=kind,
        enforcing_vendor=event.vendor,
        challenge_type=event.challenge_type,
        http_status=event.http_status,
        url_class=event.url_class,
        evidence_refs=(_ref(visit, index),),
    )


def anti_bot_services(evidence: HarEvidence, context: FailureContext) -> list[str | None]:
    """The service of each qualified failure in the attempt: a HAR event the extractor marked as enforcement, and an
    unresolved presented challenge from the run's own solves. A widget, asset or vendor merely present is none."""
    services: list[str | None] = []
    if evidence.availability == HarEvidenceAvailability.available:
        services.extend(
            event.service
            for visit in evidence.visits
            for event in visit.events
            if event.failure_signal and _in_attempt(_utc(event.event_at), context)
        )
    receipts = [
        receipt
        for receipt in run_solve_receipts(context.run_ids, context.window_start)
        if _in_attempt(_utc(receipt.ended_at), context)
    ]
    services.extend(receipt.service for receipt in unresolved_challenges(receipts))
    return services


async def final_failure_diagnostic(evidence: HarEvidence, context: FailureContext) -> FinalDiagnostic:
    diagnostic = diagnose(evidence, context)
    system_errors = tuple(error.model_dump() for error in anti_bot_system_errors(anti_bot_services(evidence, context)))
    LOG.info(
        "Final anti-bot diagnostic",
        task_id=context.task_id,
        attempt_number=context.attempt_number,
        har_availability=diagnostic.availability.value,
        enforcement=diagnostic.enforcement.value,
        system_error_codes=[error["error_code"] for error in system_errors],
    )
    return replace(diagnostic, system_errors=system_errors)


def diagnostic_entry(diagnostic: FinalDiagnostic, context: FailureContext) -> dict[str, Any] | None:
    """The failure_category entry to persist, or None when there is nothing to report beyond the log line."""
    if diagnostic.enforcement == Enforcement.none_observed and not diagnostic.system_errors:
        return None
    if diagnostic.enforcement == Enforcement.none_observed:
        reasoning = "A challenge was left unresolved during the failed attempt; it is not known to have caused it"
    else:
        vendor = diagnostic.enforcing_vendor or "Bot protection"
        reasoning = f"{vendor} enforcement was recorded during the failed attempt; it is not known to have caused it"
    entry: dict[str, Any] = {
        "category": FailureCategory.ANTI_BOT_DETECTION.value,
        "confidence_float": 0.5,
        "reason_code": HAR_ANTI_BOT_DIAGNOSTIC_REASON_CODE,
        "evidence_source": "har",
        "reasoning": reasoning,
        "anti_bot": {
            "har_availability": diagnostic.availability.value,
            "vendors_observed": list(diagnostic.vendors_observed),
            "enforcement": diagnostic.enforcement.value,
            "enforcing_vendor": diagnostic.enforcing_vendor,
            "challenge_type": diagnostic.challenge_type,
            "http_status": diagnostic.http_status,
            "url_class": diagnostic.url_class,
            "contribution": CONTRIBUTION_UNKNOWN,
            "evidence_refs": list(diagnostic.evidence_refs),
            "attempt_number": context.attempt_number,
            "failed_task_id": context.task_id,
            "failed_action_id": context.failed_action.action_id if context.failed_action else None,
            "failed_step_id": context.failed_action.step_id if context.failed_action else None,
            "failed_screenshot_artifact_id": (
                context.failed_action.screenshot_artifact_id if context.failed_action else None
            ),
            "system_errors": list(diagnostic.system_errors),
        },
    }
    return entry


def diagnostic_system_errors(categories: object, attempt_number: int | None) -> list[dict[str, Any]]:
    """The anti-bot system errors the run's diagnostic entry recorded for ``attempt_number``, re-validated so only
    the code-authored system rows this module writes can come out."""
    if not isinstance(categories, list):
        return []
    for category in categories:
        if not isinstance(category, dict) or category.get("reason_code") != HAR_ANTI_BOT_DIAGNOSTIC_REASON_CODE:
            continue
        anti_bot = category.get("anti_bot")
        if not isinstance(anti_bot, dict) or anti_bot.get("attempt_number") != attempt_number:
            return []
        rows = anti_bot.get("system_errors")
        return [
            {"error_code": row["error_code"], "reasoning": row["reasoning"], "error_type": row["error_type"]}
            for row in (rows if isinstance(rows, list) else [])
            if isinstance(row, dict)
            and row.get("error_type") == ErrorType.SYSTEM_DEFINED_ERROR.value
            and row.get("error_code") in ANTI_BOT_SYSTEM_ERROR_CODES
            and isinstance(row.get("reasoning"), str)
        ]
    return []


def final_har_user_errors(categories: object, attempt_number: int | None) -> list[dict[str, Any]]:
    """The user errors a workflow run's final match recorded for ``attempt_number``."""
    for category in categories if isinstance(categories, list) else []:
        if not is_final_har_user_match_entry(category):
            continue
        identity = category.get("identity")
        rows = category.get("user_errors")
        if not isinstance(identity, dict) or identity.get("attempt") != attempt_number or not isinstance(rows, list):
            return []
        return [row for row in rows if isinstance(row, dict) and row.get("error_type") == ErrorType.USER_DEFINED_ERROR]
    return []


def network_evidence(evidence: HarEvidence, context: FailureContext) -> str | None:
    """The attempt's ordinary network facts as class-level JSON (never a URL, host, header or body), or None."""
    if evidence.availability != HarEvidenceAvailability.available:
        return None
    http_failures = [
        {"http_status": f.status, "method": f.method, "url_class": f.url_class}
        for f in evidence.http_failures
        if _in_attempt(_utc(f.at), context)
    ]
    events = [
        {"event_type": e.event_type, "http_status": e.http_status, "url_class": e.url_class}
        for visit in _attempt_visits(evidence, context)
        for e in visit.events
        if _in_attempt(_utc(e.event_at), context)
    ]
    if not http_failures and not events:
        return None
    return json.dumps({"untrusted_recorded_facts": True, "http_failures": http_failures, "events": events})


async def _ask_user_mapping(
    *,
    organization_id: str,
    error_code_mapping: dict[str, str],
    network_evidence_str: str,
    navigation_goal: str | None,
    navigation_payload: object,
    failure_reason: str | None,
    mask: Callable[[str], str],
    secret_values: Collection[str],
) -> list[dict[str, Any]]:
    # A declared code that is itself a registered secret can be neither shown nor masked; skip the optional match.
    if any(code in secret_values for code in error_code_mapping):
        return []

    def masked(value: object) -> object:
        # Field by field before serializing: a secret JSON escapes differently, and codes and HTTP facts are structure.
        if isinstance(value, str):
            return mask(value) if value else value
        if isinstance(value, dict):
            return {masked(key): masked(item) for key, item in value.items()}
        if isinstance(value, list):
            return [masked(item) for item in value]
        return value

    prompt = prompt_engine.load_prompt(
        _USER_MATCH_PROMPT,
        navigation_goal=masked(navigation_goal),
        navigation_payload_str=json.dumps(masked(navigation_payload)),
        elements="",
        current_url="(the run has ended; no page is open)",
        action_history="[]",
        error_code_mapping_str=json.dumps({code: masked(text) for code, text in error_code_mapping.items()}),
        network_evidence_str=network_evidence_str,
        local_datetime=datetime.now(UTC).isoformat(),
        reasoning=masked(failure_reason),
    )
    async with asyncio.timeout(_USER_MATCH_TIMEOUT_S):
        response = await get_org_aware_secondary_llm_api_handler(default=app.SECONDARY_LLM_API_HANDLER)(
            prompt=prompt, prompt_name=_USER_MATCH_PROMPT, organization_id=organization_id
        )
    # Masked before parsing: the parser cuts reasoning to its bound, which would keep the head of a secret crossing it.
    raw = [
        {**error, "reasoning": mask(error["reasoning"])}
        if isinstance(error, dict) and isinstance(error.get("reasoning"), str)
        else error
        for error in response.get("errors", [])
    ]
    parsed = [UserDefinedError.model_validate(error) for error in raw]
    kept, _ = filter_to_user_defined_codes(parsed, error_code_mapping)
    rows: dict[str, dict[str, Any]] = {}
    for error in kept:
        rows.setdefault(error.error_code, error.model_dump(mode="json"))
    return list(rows.values())


def _match_identity(
    owner_id: str, organization_id: str, attempt_number: int | None, finished_at: datetime | None
) -> dict[str, Any]:
    """The execution one match is spent for; the mapping and the row it is read from are not part of it."""
    at = _utc(finished_at)
    return {
        "owner": owner_id,
        "organization": organization_id,
        "attempt": attempt_number,
        "finished_at": at.isoformat() if at else None,
    }


def _match_source(kind: str, root_id: str, error_code_mapping: dict[str, str]) -> dict[str, Any]:
    return {"kind": kind, "root": root_id, "mapping": final_har_user_mapping_digest(error_code_mapping)}


async def _match_failed_task(task: Task, evidence: HarEvidence, context: FailureContext) -> Task | None:
    assert task.organization_id
    facts = network_evidence(evidence, context)
    if not task.error_code_mapping or facts is None:
        return None
    # The task's own context holds the values it registered as secrets (a delivered verification code, say); taken
    # before the request, they mask what the model repeats of them before anything is stored.
    owner = skyvern_context.current()
    if owner is None or task.task_id not in (owner.task_id, owner.run_id):
        return None
    secret_values = frozenset(owner.runtime_secret_values)
    identity = _match_identity(task.task_id, task.organization_id, task.attempt_number, task.finished_at)
    source = _match_source("task", task.task_id, task.error_code_mapping)
    if not await app.DATABASE.tasks.claim_final_har_user_match(
        task.task_id, task.organization_id, task.finished_at, identity, source
    ):
        return None
    rows = await _ask_user_mapping(
        organization_id=task.organization_id,
        error_code_mapping=task.error_code_mapping,
        network_evidence_str=facts,
        navigation_goal=task.navigation_goal,
        navigation_payload=task.navigation_payload,
        failure_reason=task.failure_reason,
        mask=lambda text: redact_secrets_from_text(text, secret_values),
        secret_values=secret_values,
    )
    masked = [
        {**row, "reasoning": _bounded_reasoning(redact_secrets_from_text(row["reasoning"], secret_values))}
        for row in rows
    ]
    return await app.DATABASE.tasks.record_final_har_user_match(
        task.task_id, task.organization_id, task.finished_at, identity, source, masked
    )


async def annotate_failed_task(task: Task, evidence: HarEvidence) -> Task:
    """``task`` with its final diagnostic and final user-error match persisted, or unchanged. Raises only
    cancellation, which callers hold until their webhook has been attempted."""
    if task.status.value not in DIAGNOSABLE_FAILURE_STATUSES or not task.organization_id:
        return task
    try:
        context = await freeze_failure_context(
            task,
            organization_id=task.organization_id,
            finished_at=task.finished_at,
            window_start=task.started_at or task.created_at,
            attempt_number=task.attempt_number,
            run_ids=(task.task_id,),
        )
        diagnostic = await final_failure_diagnostic(evidence, context)
        entry = diagnostic_entry(diagnostic, context)
        if entry is not None:
            updated = await app.DATABASE.tasks.set_final_failure_diagnostic(
                task.task_id,
                task.organization_id,
                task.finished_at,
                entry,
                system_errors=list(diagnostic.system_errors),
            )
            task = updated or task
    except asyncio.CancelledError:
        raise
    except Exception:
        LOG.warning("Failed to record the final failure diagnostic", task_id=task.task_id, exc_info=True)
        return task
    try:
        return await _match_failed_task(task, evidence, context) or task
    except asyncio.CancelledError:
        raise
    except Exception:
        # The claim stays spent: a lost optional enrichment, never a second request.
        LOG.warning("Final recording user-error match failed", task_id=task.task_id, exc_info=True)
        return task


async def _terminal_block_task_id(workflow_run: WorkflowRun, attempt_number: int) -> str | None:
    blocks = await app.DATABASE.observer.get_workflow_run_blocks(
        workflow_run.workflow_run_id, organization_id=workflow_run.organization_id, prefer_capped=True
    )
    # Newest first; attempt-1 rows written before attempts were numbered carry no attempt number.
    for block in blocks:
        if block.attempt_number == attempt_number or (attempt_number == 1 and block.attempt_number is None):
            return None if block.continue_on_failure else block.task_id
    return None


@dataclass(frozen=True)
class FailureBeforeFinally:
    """The outcome a run held before its finally block ran, the task that owned it (None when none did), and when
    that failure ended."""

    status: WorkflowRunStatus
    failure_reason: str | None
    task_id: str | None
    finished_at: datetime | None


async def _failing_task(workflow_run: WorkflowRun, attempt_number: int) -> Task | None:
    # A task owns the failure only when it failed and is the attempt's last block, run without
    # continue_on_failure; a later non-task block failing leaves no task context to claim.
    last_task = await app.DATABASE.tasks.get_last_task_for_workflow_run(
        workflow_run.workflow_run_id, attempt_number=attempt_number
    )
    if (
        last_task
        and last_task.status.value in DIAGNOSABLE_FAILURE_STATUSES
        and last_task.task_id == await _terminal_block_task_id(workflow_run, attempt_number)
    ):
        return last_task
    return None


async def failure_before_finally(workflow_run: WorkflowRun, attempt_number: int) -> FailureBeforeFinally:
    """Read before a finally block adds blocks and tasks that would hide which task owned the failure."""
    failing = await _failing_task(workflow_run, attempt_number)
    return FailureBeforeFinally(
        workflow_run.status,
        workflow_run.failure_reason,
        failing.task_id if failing else None,
        failing.finished_at if failing and failing.finished_at else workflow_run.finished_at,
    )


def _still_holds(current: WorkflowRun, before_finally: FailureBeforeFinally | None) -> bool:
    return before_finally is not None and (current.status, current.failure_reason) == (
        before_finally.status,
        before_finally.failure_reason,
    )


async def _mapping_task(
    current: WorkflowRun, failing: Task | None, before_finally: FailureBeforeFinally | None
) -> Task | None:
    """The task whose mapping the final match answers: the one that owned the failure before the finally block ran,
    while the run still holds that exact outcome; otherwise the attempt's failing task."""
    if before_finally is None or not _still_holds(current, before_finally):
        return failing
    if before_finally.task_id is None:
        return None
    return await app.DATABASE.tasks.get_task(before_finally.task_id, organization_id=current.organization_id)


async def annotate_failed_workflow_run(
    workflow_run: WorkflowRun,
    evidence: HarEvidence,
    *,
    attempt_number: int,
    attempt_started_at: datetime | None,
    before_finally: FailureBeforeFinally | None = None,
) -> WorkflowRun:
    """``workflow_run``, the snapshot its own terminal write returned, with its final diagnostic persisted onto that
    execution's failed attempt, or unchanged. Raises only cancellation."""
    status = WorkflowRunStatus(workflow_run.status)
    if status.value not in DIAGNOSABLE_FAILURE_STATUSES or workflow_run.finished_at is None:
        return workflow_run
    try:
        # The row can have moved on since that write.
        current = await app.DATABASE.workflow_runs.get_workflow_run(
            workflow_run.workflow_run_id, organization_id=workflow_run.organization_id
        )
        # A reset reuses the row: a different finish is another execution, whose data this one must not use.
        if (
            current is None
            or current.status.value not in DIAGNOSABLE_FAILURE_STATUSES
            or current.finished_at != workflow_run.finished_at
        ):
            return workflow_run
        failing = await _failing_task(current, attempt_number)
        mapping_task = await _mapping_task(current, failing, before_finally)
        context = await freeze_failure_context(
            failing,
            organization_id=current.organization_id,
            finished_at=failing.finished_at if failing and failing.finished_at else workflow_run.finished_at,
            window_start=attempt_started_at or workflow_run.started_at or workflow_run.created_at,
            attempt_number=attempt_number,
            run_ids=(current.workflow_run_id,),
        )
        entry = diagnostic_entry(await final_failure_diagnostic(evidence, context), context)
        if entry is not None:
            updated = await app.DATABASE.workflow_runs.set_final_failure_diagnostic(
                current.workflow_run_id, current.organization_id, workflow_run.finished_at, attempt_number, entry
            )
            workflow_run = updated or workflow_run
    except asyncio.CancelledError:
        raise
    except Exception:
        LOG.warning(
            "Failed to record the final failure diagnostic", workflow_run_id=workflow_run.workflow_run_id, exc_info=True
        )
        return workflow_run
    try:
        # The preserved failure is answered with facts up to its own finish plus the grace, not the finally block's
        # later work; cleanup traffic inside that grace can still appear.
        match_context = (
            replace(context, finished_at=_utc(before_finally.finished_at))
            if before_finally is not None and _still_holds(current, before_finally) and before_finally.finished_at
            else context
        )
        matched = await _match_failed_workflow_run(workflow_run, mapping_task, evidence, match_context, attempt_number)
        return matched or workflow_run
    except asyncio.CancelledError:
        raise
    except Exception:
        # The claim stays spent: a lost optional enrichment, never a second request.
        LOG.warning(
            "Final recording user-error match failed", workflow_run_id=workflow_run.workflow_run_id, exc_info=True
        )
        return workflow_run


async def _workflow_mapping_owner(
    workflow_run: WorkflowRun, failing: Task | None
) -> tuple[str, str, Task | TaskV2] | None:
    """The mapping the failure answers to: the failing task's, else the task v2 root's. Read before the claim; the
    claim and the result each re-check it under their row locks."""
    if failing is not None:
        task = await app.DATABASE.tasks.get_task(failing.task_id, organization_id=workflow_run.organization_id)
        if task is not None and task.error_code_mapping:
            return "task", task.task_id, task
    root = await app.DATABASE.observer.get_task_v2_by_workflow_run_id(
        workflow_run.workflow_run_id, organization_id=workflow_run.organization_id
    )
    if root is not None and root.error_code_mapping:
        return "task_v2", root.observer_cruise_id, root
    return None


def _masked(rows: list[dict[str, Any]], run_context: WorkflowRunContext) -> list[dict[str, Any]]:
    """``rows`` with the run's registered secrets masked before they are stored: the webhook and API read them
    after the run's context is gone."""
    return [
        {**row, "reasoning": _bounded_reasoning(run_context.mask_secrets_in_reasoning(row["reasoning"]))}
        for row in rows
    ]


def _bounded_reasoning(masked: str) -> str:
    # Masking can lengthen valid reasoning past the bound the response's strict parser enforces, which would drop
    # the matched row; cut only after masking, and strip-clean as that parser requires.
    cut = masked[:ERROR_CODE_REASONING_MAX_LENGTH]
    return cut.rstrip() or cut


async def _match_failed_workflow_run(
    workflow_run: WorkflowRun,
    failing: Task | None,
    evidence: HarEvidence,
    context: FailureContext,
    attempt_number: int,
) -> WorkflowRun | None:
    facts = network_evidence(evidence, context)
    owner = await _workflow_mapping_owner(workflow_run, failing) if facts is not None else None
    if facts is None or owner is None:
        return None
    kind, root_id, mapping_owner = owner
    mapping: dict[str, str] = mapping_owner.error_code_mapping or {}
    identity = _match_identity(
        workflow_run.workflow_run_id, workflow_run.organization_id, attempt_number, workflow_run.finished_at
    )
    source = _match_source(kind, root_id, mapping)
    # Held from before the request: a duplicate cleanup can evict the registry entry while the model answers.
    run_context = app.WORKFLOW_CONTEXT_MANAGER.workflow_run_contexts.get(workflow_run.workflow_run_id)
    if not isinstance(run_context, WorkflowRunContext):
        return None
    runs = app.DATABASE.workflow_runs
    if not await runs.claim_final_har_user_match(
        workflow_run.workflow_run_id,
        workflow_run.organization_id,
        workflow_run.finished_at,
        attempt_number,
        identity,
        source,
    ):
        return None
    rows = await _ask_user_mapping(
        organization_id=workflow_run.organization_id,
        error_code_mapping=mapping,
        network_evidence_str=facts,
        navigation_goal=mapping_owner.navigation_goal if isinstance(mapping_owner, Task) else mapping_owner.prompt,
        navigation_payload=mapping_owner.navigation_payload if isinstance(mapping_owner, Task) else None,
        failure_reason=workflow_run.failure_reason,
        mask=run_context.mask_secrets_in_reasoning,
        secret_values={value for value in run_context.secrets.values() if isinstance(value, str)},
    )
    return await runs.record_final_har_user_match(
        workflow_run.workflow_run_id,
        workflow_run.organization_id,
        workflow_run.finished_at,
        attempt_number,
        identity,
        source,
        _masked(rows, run_context),
    )


def log_diagnostic_unavailable(run_id: str, exit_reason: str) -> None:
    """For a terminal exit that reports a run without its own browser cleanup, so no closed HAR can exist."""
    LOG.info(
        "Final anti-bot diagnostic",
        run_id=run_id,
        har_availability=HarEvidenceAvailability.not_collected.value,
        exit_reason=exit_reason,
    )
