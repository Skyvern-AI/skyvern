"""A failed run's final anti-bot diagnostic: the run HAR's class-level evidence, the run's challenge-solve receipts and
the failure facts persisted when the run failed, read together once cleanup has the closed recording. Pure code: no
model is asked.

The result is an output-only failure_category entry (HAR_ANTI_BOT_DIAGNOSTIC_REASON_CODE) carrying the anti-bot system
errors qualified evidence supports. A standalone task also appends those to its errors[]; a workflow run's response
projects them from the entry. Status, retry, credential fallback and attribution have already read the run before
this runs."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

import structlog

from skyvern.errors.errors import (
    ANTI_BOT_SYSTEM_ERROR_CODES,
    ErrorType,
    anti_bot_system_errors,
)
from skyvern.forge import app
from skyvern.forge.antibot_evidence import run_solve_receipts, unresolved_challenges
from skyvern.forge.failure_classifier import (
    DIAGNOSABLE_FAILURE_STATUSES,
    HAR_ANTI_BOT_DIAGNOSTIC_REASON_CODE,
    FailureCategory,
)
from skyvern.forge.sdk.schemas.har_evidence import (
    HarEvidence,
    HarEvidenceAvailability,
    HarEvidenceEvent,
    HarEvidenceVisit,
)
from skyvern.forge.sdk.schemas.tasks import Task
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRun, WorkflowRunStatus
from skyvern.webeye.actions.actions import ActionStatus

LOG = structlog.get_logger(__name__)

# Requests recorded just after the failure write still belong to the failing attempt.
_LATE_GRACE = timedelta(seconds=5)
_MAX_FACT_TEXT = 300
# The HAR names no action, frame or initiator, so nothing recorded ties an enforcement event to the operation that
# failed: whether it caused the failure stays unknown, however close in time it was.
CONTRIBUTION_UNKNOWN = "unknown"


class Enforcement(StrEnum):
    explicit_block = "explicit_block"
    repeat_block = "repeat_block"
    unresolved_challenge = "unresolved_challenge"
    challenge_resource_failed = "challenge_resource_failed"
    none_observed = "none_observed"


@dataclass(frozen=True)
class FailedActionFacts:
    action_id: str | None
    action_type: str
    status: str
    step_id: str | None
    step_order: int | None
    screenshot_artifact_id: str | None
    # What the agent said it meant to do: model-written, not page content, and never persisted.
    agent_intention: str | None


@dataclass(frozen=True)
class FailureContext:
    """What the failing task persisted when it failed, read back rather than re-captured from a live page."""

    organization_id: str
    task_id: str | None
    attempt_number: int | None
    window_start: datetime | None
    finished_at: datetime | None
    failure_reason: str | None
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


def _bounded(text: str | None) -> str | None:
    return text[:_MAX_FACT_TEXT] if text else None


async def freeze_failure_context(
    task: Task | None,
    *,
    organization_id: str,
    failure_reason: str | None,
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
                action_type=str(action.action_type),
                status=str(action.status),
                step_id=action.step_id,
                step_order=action.step_order,
                screenshot_artifact_id=action.screenshot_artifact_id,
                agent_intention=_bounded(action.intention or action.reasoning),
            )
    return FailureContext(
        organization_id=organization_id,
        task_id=task.task_id if task is not None else None,
        attempt_number=attempt_number,
        window_start=_utc(window_start),
        finished_at=_utc(finished_at),
        failure_reason=_bounded(failure_reason),
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


def diagnose(evidence: HarEvidence, context: FailureContext) -> tuple[FinalDiagnostic, list[HarEvidenceVisit]]:
    """What the attempt's recording observed: vendors present and the latest enforcement. Later traffic that loads
    fine neither clears nor erases an earlier enforcement; neither is evidence about the failed operation."""
    if evidence.availability != HarEvidenceAvailability.available:
        return FinalDiagnostic(availability=evidence.availability), []
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
        return FinalDiagnostic(availability=evidence.availability, vendors_observed=vendors), visits
    _, visit, index, event, kind = latest
    return (
        FinalDiagnostic(
            availability=evidence.availability,
            vendors_observed=vendors,
            enforcement=kind,
            enforcing_vendor=event.vendor,
            challenge_type=event.challenge_type,
            http_status=event.http_status,
            url_class=event.url_class,
            evidence_refs=(_ref(visit, index),),
        ),
        visits,
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
    diagnostic, _ = diagnose(evidence, context)
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


async def annotate_failed_task(task: Task, evidence: HarEvidence) -> Task:
    """``task`` with its final diagnostic persisted, or unchanged. Raises only cancellation, which callers hold until
    their webhook has been attempted."""
    if task.status.value not in DIAGNOSABLE_FAILURE_STATUSES or not task.organization_id:
        return task
    try:
        context = await freeze_failure_context(
            task,
            organization_id=task.organization_id,
            failure_reason=task.failure_reason,
            finished_at=task.finished_at,
            window_start=task.started_at or task.created_at,
            attempt_number=task.attempt_number,
            run_ids=(task.task_id,),
        )
        diagnostic = await final_failure_diagnostic(evidence, context)
        entry = diagnostic_entry(diagnostic, context)
        if entry is None:
            return task
        updated = await app.DATABASE.tasks.set_final_failure_diagnostic(
            task.task_id, task.organization_id, task.finished_at, entry, system_errors=list(diagnostic.system_errors)
        )
        return updated or task
    except asyncio.CancelledError:
        raise
    except Exception:
        LOG.warning("Failed to record the final failure diagnostic", task_id=task.task_id, exc_info=True)
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


async def annotate_failed_workflow_run(
    workflow_run: WorkflowRun,
    evidence: HarEvidence,
    *,
    attempt_number: int,
    attempt_started_at: datetime | None,
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
        # A task owns the failure only when it failed and is the attempt's last block, run without
        # continue_on_failure; a later non-task block failing leaves no task context to claim.
        last_task = await app.DATABASE.tasks.get_last_task_for_workflow_run(
            current.workflow_run_id, attempt_number=attempt_number
        )
        failing = (
            last_task
            if last_task
            and last_task.status.value in DIAGNOSABLE_FAILURE_STATUSES
            and last_task.task_id == await _terminal_block_task_id(current, attempt_number)
            else None
        )
        context = await freeze_failure_context(
            failing,
            organization_id=current.organization_id,
            failure_reason=current.failure_reason,
            finished_at=failing.finished_at if failing and failing.finished_at else workflow_run.finished_at,
            window_start=attempt_started_at or workflow_run.started_at or workflow_run.created_at,
            attempt_number=attempt_number,
            run_ids=(current.workflow_run_id,),
        )
        entry = diagnostic_entry(await final_failure_diagnostic(evidence, context), context)
        if entry is None:
            return workflow_run
        updated = await app.DATABASE.workflow_runs.set_final_failure_diagnostic(
            current.workflow_run_id, current.organization_id, workflow_run.finished_at, attempt_number, entry
        )
        return updated or workflow_run
    except asyncio.CancelledError:
        raise
    except Exception:
        LOG.warning(
            "Failed to record the final failure diagnostic", workflow_run_id=workflow_run.workflow_run_id, exc_info=True
        )
        return workflow_run


def log_diagnostic_unavailable(run_id: str, exit_reason: str) -> None:
    """For a terminal exit that reports a run without its own browser cleanup, so no closed HAR can exist."""
    LOG.info(
        "Final anti-bot diagnostic",
        run_id=run_id,
        har_availability=HarEvidenceAvailability.not_collected.value,
        exit_reason=exit_reason,
    )
