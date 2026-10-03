from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal, TypeAlias

import structlog
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from skyvern.exceptions import WorkflowNotFound, WorkflowNotFoundForWorkflowRun
from skyvern.forge import app
from skyvern.forge.sdk.cache import extraction_cache
from skyvern.forge.sdk.db.datetime_utils import naive_utc_now, to_naive_utc
from skyvern.forge.sdk.db.models import WorkflowRunAttemptModel
from skyvern.forge.sdk.db.repositories.workflow_runs import PrepareNextAttemptResult
from skyvern.forge.sdk.workflow.models.workflow import Workflow, WorkflowRun, WorkflowRunStatus
from skyvern.schemas.runs import RunStatus, WorkflowRunAttempt
from skyvern.schemas.workflows import WorkflowRetryPolicy
from skyvern.services.webhook_delivery import (
    WEBHOOK_DELIVERY_MAX_ATTEMPTS,
    WEBHOOK_DELIVERY_MAX_RETRY_AFTER_SECONDS,
    WEBHOOK_DELIVERY_RETRY_BASE_DELAY_SECONDS,
)

LOG = structlog.get_logger()

ATTEMPT_QUEUE_WRITE_MAX_ATTEMPTS = 3
ATTEMPT_QUEUE_WRITE_RETRY_DELAY_SECONDS = 0.1

RETRY_DECISION_RETRY = "retry"
RETRY_DECISION_FINAL = "final"
RETRY_DECISION_REVOKED = "revoked"
RETRY_DECISION_ABANDONED = "abandoned"
RETRY_DECISION_GRACE_SECONDS = 600
LEASE_SAFETY_MARGIN_SECONDS = 60

# Keep webhook timing in the OSS layer so the HTTP client, service-side delivery wrapper, and
# Temporal's bounded fallback grace share one replay-stable value without importing cloud code.
WORKFLOW_WEBHOOK_HTTP_TIMEOUT_SECONDS = 30.0
# Leave one additional timeout window plus the original five-second cushion after the complete
# delivery budget, so fallback cannot race the final in-flight request at the old 35-second mark.
WEBHOOK_FALLBACK_GRACE_MARGIN_SECONDS = WORKFLOW_WEBHOOK_HTTP_TIMEOUT_SECONDS + 5.0
WEBHOOK_FALLBACK_GRACE_SECONDS = (
    WEBHOOK_DELIVERY_MAX_ATTEMPTS * WORKFLOW_WEBHOOK_HTTP_TIMEOUT_SECONDS
    + (WEBHOOK_DELIVERY_MAX_ATTEMPTS - 1)
    * max(
        WEBHOOK_DELIVERY_MAX_RETRY_AFTER_SECONDS,
        WEBHOOK_DELIVERY_RETRY_BASE_DELAY_SECONDS * 2**WEBHOOK_DELIVERY_MAX_ATTEMPTS,
    )
    + WEBHOOK_FALLBACK_GRACE_MARGIN_SECONDS
)
# The policy terminal release owns these effects serially before the final webhook. Keep this
# geometry in one replay-stable OSS constant so Temporal's activity deadline, lease recovery, and
# webhook-only fallback cannot drift apart.
TERMINAL_SIDE_EFFECT_TIMEOUT_SECONDS = WORKFLOW_WEBHOOK_HTTP_TIMEOUT_SECONDS
TERMINAL_SIDE_EFFECT_COUNT = 5
TERMINAL_RELEASE_TOTAL_BOUND_SECONDS = (
    TERMINAL_SIDE_EFFECT_COUNT * TERMINAL_SIDE_EFFECT_TIMEOUT_SECONDS
    + WEBHOOK_FALLBACK_GRACE_SECONDS
    + LEASE_SAFETY_MARGIN_SECONDS
)
# An interim release either records the no-webhook marker (one ordinary side effect) or delivers
# the every-attempt webhook. The latter is the larger bound and therefore covers both paths.
INTERIM_RELEASE_TOTAL_BOUND_SECONDS = max(
    TERMINAL_SIDE_EFFECT_TIMEOUT_SECONDS,
    WEBHOOK_FALLBACK_GRACE_SECONDS,
)
# The once-only tail is guarded as one unit: the hook and final webhook must both fit before a
# takeover may restart the sequence. The completion marker is written by the webhook effect.
TERMINAL_SIDE_EFFECT_TAIL_BOUND_SECONDS = TERMINAL_SIDE_EFFECT_TIMEOUT_SECONDS + WEBHOOK_FALLBACK_GRACE_SECONDS
# This is the only age at which a competing owner may take over a terminal-release lease. The
# default retry backoff is capped at 100 seconds, so 15 attempts span this bound even after the
# original owner's 60-second heartbeat timeout.
LEASE_TAKEOVER_SECONDS = max(
    RETRY_DECISION_GRACE_SECONDS,
    TERMINAL_RELEASE_TOTAL_BOUND_SECONDS + LEASE_SAFETY_MARGIN_SECONDS,
)
assert TERMINAL_RELEASE_TOTAL_BOUND_SECONDS + LEASE_SAFETY_MARGIN_SECONDS <= LEASE_TAKEOVER_SECONDS
assert INTERIM_RELEASE_TOTAL_BOUND_SECONDS + LEASE_SAFETY_MARGIN_SECONDS <= LEASE_TAKEOVER_SECONDS
TERMINAL_RELEASE_RETRY_MAX_ATTEMPTS = 15
# Each reserved delivery call makes at most three HTTP requests, for at most 45 requests per kind and attempt.
WORKFLOW_WEBHOOK_DELIVERY_MAX_ATTEMPTS = 15

TerminalSideEffectOutcome: TypeAlias = Literal[
    "released",
    "already_released",
    "lease_held_by_other_young",
    "fenced_out",
    "effect_failed",
]


@dataclass(frozen=True)
class RetryDecision:
    retry: bool
    attempt_number: int
    delay_seconds: int
    send_webhook_now: bool
    decision_reason: str | None
    next_attempt_at: datetime | None = None


def remaining_retry_delay_seconds(decision: RetryDecision, *, now: datetime | None = None) -> float:
    """Seconds until the recorded next attempt; the policy delay when no time was recorded."""
    next_attempt_at = to_naive_utc(decision.next_attempt_at)
    if next_attempt_at is None:
        return float(decision.delay_seconds)
    return max(0.0, (next_attempt_at - (now or naive_utc_now())).total_seconds())


def evaluate_retry_policy(
    policy: WorkflowRetryPolicy,
    status: WorkflowRunStatus,
    error_codes: set[str],
    retries_used: int,
) -> tuple[bool, str]:
    if status is WorkflowRunStatus.canceled:
        return False, "canceled"
    if retries_used >= policy.max_retries:
        return False, "budget_exhausted"

    for rule in policy.retry_on:
        if rule.status == status.value and (not rule.error_codes or error_codes.intersection(rule.error_codes)):
            return True, "matched"
    return False, "no_match"


@dataclass(frozen=True)
class AttemptView:
    attempt: int
    retry_pending: bool
    next_attempt_at: datetime | None
    attempts: list[WorkflowRunAttempt]


AttemptRecord: TypeAlias = WorkflowRunAttemptModel


async def _get_retry_policy(
    workflow_run: WorkflowRun,
    workflow: Workflow | None = None,
) -> WorkflowRetryPolicy | None:
    if workflow is None:
        try:
            workflow = await app.WORKFLOW_SERVICE.get_workflow_by_workflow_run_id(
                workflow_run_id=workflow_run.workflow_run_id,
                organization_id=workflow_run.organization_id,
                filter_deleted=False,
            )
        except (WorkflowNotFound, WorkflowNotFoundForWorkflowRun):
            return None
    return resolve_workflow_retry_policy(workflow)


def resolve_workflow_retry_policy(workflow: Workflow | None) -> WorkflowRetryPolicy | None:
    """Resolve policy presence from the pinned workflow definition, not attempt rows."""
    if workflow is None:
        return None
    definition = getattr(workflow, "workflow_definition", None)
    policy = getattr(definition, "retry_policy", None)
    return policy if isinstance(policy, WorkflowRetryPolicy) else None


def _attempt_from_record(record: AttemptRecord) -> WorkflowRunAttempt:
    final_progress = record.final_side_effects_progress or {}
    interim_progress = record.interim_side_effects_progress or {}
    final_delivery_attempted = final_progress.get("webhook_delivery_attempted") is True and not final_progress.get(
        "webhook_delivery_exhausted_at"
    )
    interim_delivery_attempted = interim_progress.get(
        "webhook_delivery_attempted"
    ) is True and not interim_progress.get("webhook_delivery_exhausted_at")
    return WorkflowRunAttempt(
        attempt_number=record.attempt_number,
        status=record.status.value if isinstance(record.status, WorkflowRunStatus) else record.status,
        failure_reason=record.failure_reason,
        error_codes=list(record.error_codes or []),
        started_at=record.started_at,
        finished_at=record.finished_at,
        retry_decision=record.retry_decision,
        decision_reason=record.decision_reason,
        next_attempt_at=record.next_attempt_at,
        webhook_sent_at=(record.webhook_sent_at if final_delivery_attempted else None)
        or (record.interim_webhook_sent_at if interim_delivery_attempted else None),
    )


def is_retry_pending(
    status: WorkflowRunStatus | RunStatus,
    finished_at: datetime | None,
    latest_attempt: AttemptRecord,
) -> bool:
    """Return the retry-pending state shared by detail and list responses."""
    if not status.is_final():
        return False

    retry_decision = latest_attempt.retry_decision
    if retry_decision == RETRY_DECISION_RETRY:
        return latest_attempt.next_attempt_prepared_at is None
    if retry_decision is not None:
        return False

    finished_at = to_naive_utc(finished_at)
    return finished_at is not None and abs(naive_utc_now() - finished_at) <= timedelta(
        seconds=RETRY_DECISION_GRACE_SECONDS
    )


def latest_attempt_awaiting_preparation(attempts: Sequence[AttemptRecord]) -> AttemptRecord | None:
    """The newest attempt whose recorded retry has not been prepared yet."""
    latest = max(attempts, key=lambda row: row.attempt_number, default=None)
    if latest is None or latest.retry_decision != RETRY_DECISION_RETRY or latest.next_attempt_prepared_at is not None:
        return None
    return latest


def _is_retry_pending(run: WorkflowRun, latest_attempt: AttemptRecord) -> bool:
    return is_retry_pending(run.status, run.finished_at, latest_attempt)


def compute_attempt_view(
    run: WorkflowRun,
    attempt_rows: Sequence[AttemptRecord],
    policy: WorkflowRetryPolicy | None = None,
    target_attempt_number: int | None = None,
) -> AttemptView:
    if not attempt_rows:
        return AttemptView(
            attempt=1,
            retry_pending=False,
            next_attempt_at=None,
            attempts=[
                WorkflowRunAttempt(
                    attempt_number=1,
                    status=run.status.value,
                    failure_reason=run.failure_reason,
                    error_codes=[],
                    started_at=run.started_at,
                    finished_at=run.finished_at,
                )
            ],
        )

    ordered_rows = sorted(attempt_rows, key=lambda row: row.attempt_number)
    historical = target_attempt_number is not None and target_attempt_number < ordered_rows[-1].attempt_number
    if target_attempt_number is not None and historical:
        ordered_rows = [row for row in ordered_rows if row.attempt_number <= target_attempt_number]
        if not ordered_rows or ordered_rows[-1].attempt_number != target_attempt_number:
            raise ValueError(f"Workflow attempt {target_attempt_number} not found")
    last = ordered_rows[-1]
    attempts = [_attempt_from_record(row) for row in ordered_rows]

    if last.finished_at is None and not historical:
        live_attempt = attempts[-1]
        attempts[-1] = WorkflowRunAttempt(
            attempt_number=live_attempt.attempt_number,
            status=run.status.value,
            failure_reason=run.failure_reason,
            error_codes=live_attempt.error_codes,
            started_at=run.started_at,
            finished_at=run.finished_at,
            retry_decision=live_attempt.retry_decision,
            decision_reason=live_attempt.decision_reason,
            next_attempt_at=live_attempt.next_attempt_at,
            webhook_sent_at=live_attempt.webhook_sent_at,
        )

    # Historical interim delivery describes the retry decision before its successor was prepared.
    retry_pending = last.retry_decision == RETRY_DECISION_RETRY if historical else _is_retry_pending(run, last)
    return AttemptView(
        attempt=last.attempt_number,
        retry_pending=retry_pending,
        # A revoked or abandoned row keeps its scheduled time for recovery bookkeeping only.
        next_attempt_at=last.next_attempt_at if retry_pending else None,
        attempts=attempts,
    )


def is_top_level_run(workflow_run: WorkflowRun) -> bool:
    # Mirrors the top-level exclusions in credential_fallback.py.
    return not (
        getattr(workflow_run, "parent_workflow_run_id", None)
        or getattr(workflow_run, "debug_session_id", None)
        or getattr(workflow_run, "copilot_session_id", None)
    )


async def is_retry_eligible_run(
    workflow_run: WorkflowRun,
    organization_id: str,
    workflow: Workflow | None = None,
) -> bool:
    if not is_top_level_run(workflow_run):
        return False
    if await _get_retry_policy(workflow_run, workflow) is None:
        return False

    is_block_scoped_run = await app.AGENT_FUNCTION.is_block_scoped_workflow_run(workflow_run)
    if not is_block_scoped_run:
        is_block_scoped_run = await app.DATABASE.debug.has_block_run_for_workflow_run(
            organization_id=organization_id,
            workflow_run_id=workflow_run.workflow_run_id,
        )
    return not is_block_scoped_run


async def ensure_attempt_row(
    workflow_run: WorkflowRun,
    organization_id: str,
    requested_browser_session_id: str | None,
    workflow: Workflow | None = None,
) -> bool:
    if not await is_retry_eligible_run(workflow_run, organization_id, workflow):
        return False

    attempts = await app.DATABASE.workflow_run_attempts.get_attempts(workflow_run.workflow_run_id)
    if any(attempt.attempt_number == 1 for attempt in attempts):
        return True

    try:
        await app.DATABASE.workflow_run_attempts.create_attempt(
            workflow_run_id=workflow_run.workflow_run_id,
            organization_id=organization_id,
            attempt_number=1,
            status=workflow_run.status.value,
            pinned_browser_session_id=requested_browser_session_id,
        )
    except IntegrityError:
        # A concurrent setup call may have committed the same primary key between the read and
        # insert. Confirm that exact row before treating the conflict as the idempotent success.
        attempts = await app.DATABASE.workflow_run_attempts.get_attempts(workflow_run.workflow_run_id)
        if not any(attempt.attempt_number == 1 for attempt in attempts):
            raise
    return True


async def fail_run_without_attempt_row(
    workflow_run_id: str,
    failure_reason: str,
    *,
    api_key: str | None = None,
    need_call_webhook: bool = True,
    cascade_children: bool = False,
) -> bool:
    """Fail a queued run that no recovery sweep can see. Returns False when an attempt row exists."""
    if await app.DATABASE.workflow_run_attempts.get_attempts(workflow_run_id):
        return False
    try:
        workflow_run = await app.WORKFLOW_SERVICE.mark_workflow_run_as_failed_if_not_final(
            workflow_run_id=workflow_run_id,
            failure_reason=failure_reason,
            cascade_children=cascade_children,
        )
    except Exception as finalization_error:
        try:
            workflow_run = await app.DATABASE.workflow_runs.get_workflow_run(workflow_run_id)
        except Exception:
            LOG.warning(
                "Failed to re-read workflow run after a finalization error; leaving the run recoverable",
                workflow_run_id=workflow_run_id,
                exc_info=True,
            )
            raise finalization_error
        if workflow_run is not None and not workflow_run.status.is_final():
            raise
        LOG.warning(
            "Workflow run is terminal after initialization failure despite a finalization error",
            workflow_run_id=workflow_run_id,
            exc_info=True,
        )
        if workflow_run is not None and (
            workflow_run.status != WorkflowRunStatus.failed or workflow_run.failure_reason != failure_reason
        ):
            workflow_run = None
    if workflow_run is not None and need_call_webhook:
        try:
            await app.WORKFLOW_SERVICE.execute_workflow_webhook(workflow_run, api_key=api_key, claim_kind=None)
        except Exception:
            LOG.warning(
                "Failed to deliver workflow webhook after initialization failure",
                workflow_run_id=workflow_run_id,
                exc_info=True,
            )
    return True


async def queue_initial_attempt(workflow_run_id: str, attempt_number: int) -> bool:
    """Queue a new run's durable rows before its dispatch, retrying a failed write.

    Dispatch recovery claims only a queued, unstarted run whose attempt is queued, so both rows move in one
    transaction. False means the run is no longer dispatchable. The last write failure is re-raised instead of
    letting a dispatch start that a restart cannot recover.
    """
    for queue_attempt in range(1, ATTEMPT_QUEUE_WRITE_MAX_ATTEMPTS + 1):
        try:
            queued = await app.DATABASE.workflow_runs.queue_initial_dispatch(workflow_run_id, attempt_number)
        except asyncio.CancelledError:
            raise
        except Exception:
            if queue_attempt == ATTEMPT_QUEUE_WRITE_MAX_ATTEMPTS:
                LOG.warning(
                    "Failed to mark the initial workflow attempt queued; the dispatch was not started",
                    workflow_run_id=workflow_run_id,
                    attempt_number=attempt_number,
                    queue_attempt=queue_attempt,
                )
                raise
            delay_seconds = ATTEMPT_QUEUE_WRITE_RETRY_DELAY_SECONDS * queue_attempt
            LOG.warning(
                "Failed to mark the initial workflow attempt queued; retrying before dispatch",
                workflow_run_id=workflow_run_id,
                attempt_number=attempt_number,
                queue_attempt=queue_attempt,
                delay_seconds=delay_seconds,
                exc_info=True,
            )
            await asyncio.sleep(delay_seconds)
            continue
        if not queued:
            LOG.info(
                "Workflow run is no longer dispatchable; skipping the dispatch",
                workflow_run_id=workflow_run_id,
                attempt_number=attempt_number,
            )
        return queued
    raise AssertionError("unreachable")


async def mark_attempt_started(workflow_run_id: str, attempt_number: int) -> bool:
    """Record the attempt start; False when the row is missing, already started, or finalized."""
    now = naive_utc_now()
    async with app.DATABASE.workflow_run_attempts.Session() as session:
        result = await session.execute(
            update(WorkflowRunAttemptModel)
            .where(
                WorkflowRunAttemptModel.workflow_run_id == workflow_run_id,
                WorkflowRunAttemptModel.attempt_number == attempt_number,
                WorkflowRunAttemptModel.started_at.is_(None),
                # finalize_attempt never sets started_at, so a cancel that lands between the run's
                # running write and this one must not be overwritten back to running.
                WorkflowRunAttemptModel.retry_decision.is_(None),
            )
            .values(started_at=now, status=WorkflowRunStatus.running.value, modified_at=now)
        )
        await session.commit()
        return bool(result.rowcount)


def _decision_from_attempt(
    attempt: AttemptRecord,
    policy: WorkflowRetryPolicy | None,
) -> RetryDecision:
    decision = attempt.retry_decision
    retry = decision == RETRY_DECISION_RETRY
    delay_seconds = policy.delay_seconds if policy is not None and retry else 0
    send_webhook_now = retry and policy is not None and policy.webhook_on_retry == "every_attempt"
    return RetryDecision(
        retry=retry,
        attempt_number=attempt.attempt_number,
        delay_seconds=delay_seconds,
        send_webhook_now=send_webhook_now or not retry,
        decision_reason=attempt.decision_reason,
        next_attempt_at=attempt.next_attempt_at if retry else None,
    )


async def get_recorded_decision(
    workflow_run_id: str,
    attempt_number: int | None = None,
) -> RetryDecision | None:
    attempts = await app.DATABASE.workflow_run_attempts.get_attempts(workflow_run_id)
    if not attempts:
        return None
    attempt = (
        next((row for row in attempts if row.attempt_number == attempt_number), None)
        if attempt_number is not None
        else max(attempts, key=lambda row: row.attempt_number)
    )
    if attempt is None:
        return None
    if attempt.retry_decision is None:
        return None
    workflow_run = await app.DATABASE.workflow_runs.get_workflow_run(workflow_run_id)
    policy = await _get_retry_policy(workflow_run) if workflow_run is not None else None
    return _decision_from_attempt(attempt, policy)


async def on_terminal_transition(
    workflow_run: WorkflowRun,
    status: WorkflowRunStatus,
    failure_reason: str | None,
    failure_category: list[dict[str, Any]] | None,
    attempt_number: int | None = None,
    *,
    refresh_finished_at: bool = True,
) -> RetryDecision:
    if not status.is_final():
        raise ValueError(f"Cannot record retry decision for non-terminal status: {status.value}")

    attempts = await app.DATABASE.workflow_run_attempts.get_attempts(workflow_run.workflow_run_id)
    if not attempts:
        return RetryDecision(False, attempt_number or 1, 0, True, "no_attempt_row")

    attempt = (
        next((row for row in attempts if row.attempt_number == attempt_number), None)
        if attempt_number is not None
        else max(attempts, key=lambda row: row.attempt_number)
    )
    if attempt is None:
        return RetryDecision(False, attempt_number or 1, 0, True, "attempt_not_found")
    attempt_number = attempt.attempt_number
    policy = await _get_retry_policy(workflow_run)
    existing_decision = attempt.retry_decision
    if (
        existing_decision is not None
        and existing_decision != RETRY_DECISION_REVOKED
        and (existing_decision != RETRY_DECISION_ABANDONED or attempt.finished_at is not None)
    ):
        # A concurrent writer may have recorded the decision after recovery began. The database
        # finalize_attempt CAS covers a later race; this branch must also avoid refreshing it.
        if not refresh_finished_at:
            return _decision_from_attempt(attempt, policy)
        # A finally block reopens the run and terminalizes it again. The decision stays frozen, but
        # the attempt's end time must cover that work: compute cost is priced through finished_at.
        refreshed = await app.DATABASE.workflow_run_attempts.refresh_attempt_finished_at(
            workflow_run.workflow_run_id, attempt_number, finished_at=naive_utc_now()
        )
        return _decision_from_attempt(refreshed or attempt, policy)

    tasks, blocks = await asyncio.gather(
        app.DATABASE.tasks.get_tasks_by_workflow_run_id(workflow_run.workflow_run_id),
        app.DATABASE.observer.get_workflow_run_blocks(
            workflow_run_id=workflow_run.workflow_run_id,
            organization_id=workflow_run.organization_id,
        ),
    )
    error_codes: set[str] = set()
    for task in tasks:
        task_attempt = task.attempt_number or 1
        if task_attempt != attempt_number:
            continue
        for error in task.errors:
            error_code = error.get("error_code")
            if isinstance(error_code, str):
                error_codes.add(error_code)
    for block in blocks:
        block_attempt = block.attempt_number or 1
        if block_attempt != attempt_number:
            continue
        error_codes.update(code for code in (block.error_codes or []) if isinstance(code, str))

    finished_at = naive_utc_now()

    if existing_decision == RETRY_DECISION_ABANDONED:
        await app.DATABASE.workflow_run_attempts.revoke_or_abandon_attempt(
            workflow_run_id=workflow_run.workflow_run_id,
            attempt_number=attempt_number,
            decision=RETRY_DECISION_ABANDONED,
            reason=attempt.decision_reason or RETRY_DECISION_ABANDONED,
            status=status.value,
            failure_reason=failure_reason,
            failure_category=failure_category,
            error_codes=sorted(error_codes),
            finished_at=finished_at,
            lease_takeover_seconds=LEASE_TAKEOVER_SECONDS,
        )
        refreshed_attempts = await app.DATABASE.workflow_run_attempts.get_attempts(workflow_run.workflow_run_id)
        refreshed_attempt = next((row for row in refreshed_attempts if row.attempt_number == attempt_number), attempt)
        return _decision_from_attempt(refreshed_attempt, policy)

    retry = False
    decision_reason: str | None = "no_match"
    next_attempt_at: datetime | None = None
    retry_decision = RETRY_DECISION_FINAL
    if existing_decision == RETRY_DECISION_REVOKED:
        decision_reason = attempt.decision_reason
        next_attempt_at = attempt.next_attempt_at
        retry_decision = RETRY_DECISION_REVOKED
    elif attempt.decision_reason == "unrecoverable_block_outputs":
        decision_reason = attempt.decision_reason
        retry_decision = RETRY_DECISION_ABANDONED
    elif policy is not None:
        retries_used = sum(1 for row in attempts if row.retry_decision == RETRY_DECISION_RETRY)
        retry, decision_reason = evaluate_retry_policy(policy, status, error_codes, retries_used)
        if retry:
            next_attempt_at = naive_utc_now() + timedelta(seconds=policy.delay_seconds)
            retry_decision = RETRY_DECISION_RETRY

    finalized = await app.DATABASE.workflow_run_attempts.finalize_attempt(
        workflow_run_id=workflow_run.workflow_run_id,
        attempt_number=attempt_number,
        status=status.value,
        failure_reason=failure_reason,
        failure_category=failure_category,
        error_codes=sorted(error_codes),
        finished_at=finished_at,
        retry_decision=retry_decision,
        decision_reason=None if retry else decision_reason,
        next_attempt_at=next_attempt_at,
    )
    return _decision_from_attempt(finalized, policy)


async def prepare_next_attempt_result(
    workflow_run_id: str,
    organization_id: str,
    from_attempt: int,
    clear_browser_address: bool = True,
    replacement_browser_address: str | None = None,
) -> PrepareNextAttemptResult:
    attempts = await app.DATABASE.workflow_run_attempts.get_attempts(workflow_run_id)
    current_attempt = next(
        (row for row in attempts if row.attempt_number == from_attempt),
        None,
    )
    workflow_run = await app.DATABASE.workflow_runs.get_workflow_run(
        workflow_run_id=workflow_run_id,
        organization_id=organization_id,
    )
    if current_attempt is None or workflow_run is None:
        return PrepareNextAttemptResult(status="failed")

    pinned_browser_session_id = current_attempt.pinned_browser_session_id
    preparation = await app.DATABASE.workflow_runs.prepare_next_attempt_atomic(
        workflow_run_id=workflow_run_id,
        organization_id=organization_id,
        from_attempt=from_attempt,
        expected_status=workflow_run.status,
        browser_session_id=pinned_browser_session_id,
        clear_browser_address=clear_browser_address,
        replacement_browser_address=replacement_browser_address,
    )
    if preparation.status != "inserted":
        return preparation

    app.WORKFLOW_CONTEXT_MANAGER.remove_workflow_run_context(workflow_run_id)
    extraction_cache.clear_workflow_run(workflow_run_id)
    return preparation


async def finalize_abandoned_attempt(
    workflow_run_id: str,
    organization_id: str,
    reason: str,
    status: WorkflowRunStatus | str | None = None,
    failure_reason: str | None = None,
    finished_at: datetime | None = None,
    attempt_number: int | None = None,
) -> RetryDecision:
    workflow_run: WorkflowRun | None = None
    if status is None or failure_reason is None or finished_at is None:
        workflow_run = await app.DATABASE.workflow_runs.get_workflow_run(
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        )
        if workflow_run is not None and workflow_run.status.is_final():
            if status is None:
                status = workflow_run.status
            if failure_reason is None:
                failure_reason = workflow_run.failure_reason
            if finished_at is None:
                finished_at = workflow_run.finished_at

    attempts = await app.DATABASE.workflow_run_attempts.get_attempts(workflow_run_id)
    if not attempts:
        return RetryDecision(False, attempt_number or 1, 0, True, reason)

    target_attempt = (
        next((row for row in attempts if row.attempt_number == attempt_number), None)
        if attempt_number is not None
        else max(attempts, key=lambda row: row.attempt_number)
    )
    if target_attempt is None:
        return RetryDecision(False, attempt_number or 1, 0, True, "attempt_not_found")
    attempt_number = target_attempt.attempt_number
    abandoned_attempt = await app.DATABASE.workflow_run_attempts.revoke_or_abandon_attempt(
        workflow_run_id=workflow_run_id,
        attempt_number=attempt_number,
        decision=RETRY_DECISION_ABANDONED,
        reason=reason,
        status=status.value if isinstance(status, WorkflowRunStatus) else status,
        failure_reason=failure_reason,
        finished_at=finished_at,
        lease_takeover_seconds=LEASE_TAKEOVER_SECONDS,
    )

    if abandoned_attempt:
        return RetryDecision(
            retry=False,
            attempt_number=abandoned_attempt.attempt_number,
            delay_seconds=0,
            send_webhook_now=True,
            decision_reason=abandoned_attempt.decision_reason or reason,
        )

    # A concurrent preparation or cancellation may have finalized this exact row after the
    # snapshot above. Preserve that durable decision instead of converting it to abandonment or
    # accidentally reading a newer attempt's row.
    refreshed_attempts = await app.DATABASE.workflow_run_attempts.get_attempts(workflow_run_id)
    refreshed_attempt = next(
        (row for row in refreshed_attempts if row.attempt_number == attempt_number),
        target_attempt,
    )
    if workflow_run is None:
        workflow_run = await app.DATABASE.workflow_runs.get_workflow_run(
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        )
    policy = await _get_retry_policy(workflow_run) if workflow_run is not None else None
    return _decision_from_attempt(refreshed_attempt, policy)
