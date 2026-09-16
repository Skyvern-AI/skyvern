import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import structlog
from fastapi import BackgroundTasks, Request

from skyvern.config import settings
from skyvern.exceptions import (
    BackgroundSequentialCredentialUnsupported,
    OrganizationNotFound,
)
from skyvern.forge import app
from skyvern.forge.sdk.api.llm.custom_llm_registry import prepare_org_llm_runtime
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.db.datetime_utils import naive_utc_now, to_naive_utc
from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
from skyvern.forge.sdk.db.repositories.workflow_run_attempts import (
    ATTEMPT_RECOVERY_BATCH_SIZE,
    ATTEMPT_RECOVERY_MAX_PAGES,
)
from skyvern.forge.sdk.executor.async_executor import AsyncExecutor
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.schemas.persistent_browser_sessions import FORCED_WORKFLOW_SESSION_RUNNABLE_TYPE
from skyvern.forge.sdk.schemas.task_v2 import TaskV2Status
from skyvern.forge.sdk.schemas.tasks import TaskStatus
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus
from skyvern.forge.sdk.workflow.retry_policy import (
    LEASE_TAKEOVER_SECONDS,
    RetryDecision,
    fail_run_without_attempt_row,
    finalize_abandoned_attempt,
    get_recorded_decision,
    latest_attempt_awaiting_preparation,
    prepare_next_attempt_result,
    queue_initial_attempt,
)
from skyvern.schemas.browser_session_close import BrowserSessionCloseReason
from skyvern.schemas.runs import RunEngine, RunType
from skyvern.services import script_service, task_v2_service
from skyvern.utils.files import initialize_skyvern_state_file

LOG = structlog.get_logger()


async def _run_with_own_context(
    func: Callable[..., Coroutine[Any, Any, Any]],
    /,
    *args: Any,
    **kwargs: Any,
) -> None:
    """Give a fire-and-forget run its own SkyvernContext instance.

    asyncio.create_task copies the ContextVar binding but not the object it points at, and the
    caller keeps running (WorkflowTriggerBlock dispatches a child and resumes). Both would then
    write the same context — execute_workflow assigns generate_script on it, block execution
    assigns task_id/step_id — and clobber each other. Shallow-copy so scalar writes stay local
    while inherited values survive.
    """
    parent = skyvern_context.current()
    if parent is not None:
        skyvern_context.set(replace(parent))
    await func(*args, **kwargs)


class BackgroundTaskExecutor(AsyncExecutor):
    # Prevent GC of fire-and-forget asyncio tasks (e.g. when running without FastAPI BackgroundTasks).
    _background_tasks: set[asyncio.Task] = set()  # noqa: RUF012

    def __init__(self) -> None:
        self._retry_recovery_sweep_task: asyncio.Task | None = None
        self._fresh_retries_recovered = False
        self._retry_resume_tasks: set[asyncio.Task] = set()
        self._scheduled_retry_resumes: set[tuple[str, int]] = set()
        self._retry_resumes_needing_recovery: set[tuple[str, int]] = set()
        self._retry_dispatches_needing_recovery: dict[tuple[str, int], tuple[Any, dict[str, Any]]] = {}

    async def recover_pending_retries(self) -> None:
        # The lifespan only logs a failed initial pass; the periodic sweep must start regardless.
        try:
            await self._recover_pending_retries_once(resume_fresh=True)
            self._fresh_retries_recovered = True
        finally:
            self._start_retry_recovery_sweep()

    async def stop_retry_recovery(self) -> None:
        tasks = [
            task
            for task in (self._retry_recovery_sweep_task, *self._retry_resume_tasks)
            if task is not None and not task.done()
        ]
        if not tasks:
            return
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def _start_retry_recovery_sweep(self) -> None:
        if self._retry_recovery_sweep_task is not None and not self._retry_recovery_sweep_task.done():
            return
        task = asyncio.create_task(self._retry_recovery_sweep())
        self._retry_recovery_sweep_task = task
        self._background_tasks.add(task)

        def clear_sweep(completed_task: asyncio.Task) -> None:
            self._background_tasks.discard(completed_task)
            if self._retry_recovery_sweep_task is completed_task:
                self._retry_recovery_sweep_task = None

        task.add_done_callback(clear_sweep)

    async def _retry_recovery_sweep(self) -> None:
        while True:
            await asyncio.sleep(LEASE_TAKEOVER_SECONDS)
            try:
                # Fresh rows found at startup stay unowned until one pass resumes them; later passes leave
                # fresh rows to their in-process owners.
                await self._recover_pending_retries_once(resume_fresh=not self._fresh_retries_recovered)
                self._fresh_retries_recovered = True
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("Retry recovery sweep failed")

    async def _recover_pending_retries_once(self, *, resume_fresh: bool) -> None:
        # Retry failed local entries immediately; process-loss recovery waits for the dispatch grace.
        for attempt, _execution_kwargs in list(self._retry_dispatches_needing_recovery.values()):
            self._schedule_retry_resume(attempt)

        await self._recover_stale_dispatch_claims()

        async def process_attempt(attempt: Any, _release_only: bool) -> None:
            if attempt.retry_decision == "retry":
                next_attempt_at = getattr(attempt, "next_attempt_at", None)
                if next_attempt_at is None:
                    return
                next_attempt_at = to_naive_utc(next_attempt_at)
                if next_attempt_at is None:
                    return
                key = (attempt.workflow_run_id, attempt.attempt_number)
                if key in self._scheduled_retry_resumes:
                    return
                if key in self._retry_resumes_needing_recovery:
                    self._schedule_retry_resume(attempt)
                    return
                cutoff = naive_utc_now() - timedelta(seconds=LEASE_TAKEOVER_SECONDS)
                if next_attempt_at < cutoff:
                    await self._abandon_stale_retry(attempt)
                elif resume_fresh:
                    self._schedule_retry_resume(attempt)
            elif attempt.retry_decision is None and attempt.started_at is not None:
                recovered = await app.WORKFLOW_SERVICE.recover_undecided_terminal_attempt(attempt)
                if recovered is not None:
                    self._schedule_retry_resume(recovered)
            elif (
                attempt.retry_decision is None
                and (terminal_run := await app.WORKFLOW_SERVICE.terminal_workflow_run(attempt)) is not None
            ):
                recovered = await app.WORKFLOW_SERVICE.recover_undecided_terminal_attempt(
                    attempt, workflow_run=terminal_run
                )
                if recovered is not None:
                    self._schedule_retry_resume(recovered)
            elif (
                attempt.retry_decision is None
                and attempt.status == WorkflowRunStatus.queued.value
                and attempt.started_at is None
            ):
                modified_at = to_naive_utc(attempt.modified_at)
                if modified_at is not None and modified_at < naive_utc_now() - timedelta(
                    seconds=LEASE_TAKEOVER_SECONDS
                ):
                    self._schedule_retry_resume(attempt)
            elif attempt.retry_decision in {"final", "revoked", "abandoned"}:
                self._retry_resumes_needing_recovery.discard((attempt.workflow_run_id, attempt.attempt_number))
                await self._release_unreleased_terminal_effects(attempt)

        await app.WORKFLOW_SERVICE.recover_pending_workflow_attempts(process_attempt=process_attempt)

    async def _recover_stale_dispatch_claims(self) -> None:
        grace_seconds = max(600, LEASE_TAKEOVER_SECONDS, settings.RETRY_DISPATCH_GRACE_SECONDS)
        stale_before = naive_utc_now() - timedelta(seconds=grace_seconds)
        cursor: tuple[str, int] | None = None
        for _page_number in range(ATTEMPT_RECOVERY_MAX_PAGES):
            attempts = await app.DATABASE.workflow_run_attempts.list_stale_dispatch_claims(stale_before, cursor=cursor)
            if not attempts:
                break
            for attempt in attempts:
                # A resumer for the previous attempt can already be dispatching this run's next attempt.
                if any(run_id == attempt.workflow_run_id for run_id, _number in self._scheduled_retry_resumes):
                    continue
                try:
                    released = await app.DATABASE.workflow_run_attempts.release_stale_dispatch_claim(
                        attempt.workflow_run_id,
                        attempt.organization_id,
                        attempt.attempt_number,
                        stale_before=stale_before,
                    )
                    if not released:
                        continue
                    attempt.started_at = None
                    attempt.status = WorkflowRunStatus.queued.value
                    self._schedule_retry_resume(attempt)
                except Exception:
                    LOG.exception(
                        "Failed to recover stale workflow retry dispatch claim",
                        workflow_run_id=attempt.workflow_run_id,
                        attempt_number=attempt.attempt_number,
                    )
            last_attempt = attempts[-1]
            cursor = (last_attempt.workflow_run_id, last_attempt.attempt_number)
            if len(attempts) < ATTEMPT_RECOVERY_BATCH_SIZE:
                break
        else:
            LOG.warning("Stale workflow retry dispatch recovery reached its page limit")

    async def _abandon_stale_retry(self, attempt: Any) -> None:
        await self._abandon_retry(attempt, reason="process_restart")

    async def _abandon_retry(self, attempt: Any, *, reason: str) -> None:
        workflow_run = await app.DATABASE.workflow_runs.get_workflow_run(
            workflow_run_id=attempt.workflow_run_id,
            organization_id=attempt.organization_id,
        )
        if workflow_run is None:
            LOG.warning(
                "Cannot recover stale retry because its workflow run is missing",
                workflow_run_id=attempt.workflow_run_id,
                attempt_number=attempt.attempt_number,
            )
            return

        if reason == "process_restart":
            execution_status = await app.AGENT_FUNCTION.get_workflow_run_execution_status(workflow_run)
            if execution_status not in {"absent", "terminal"}:
                return
        is_final = workflow_run.status.is_final()
        decision = await finalize_abandoned_attempt(
            workflow_run_id=attempt.workflow_run_id,
            organization_id=attempt.organization_id,
            reason=reason,
            status=workflow_run.status if is_final else None,
            failure_reason=workflow_run.failure_reason if is_final else None,
            finished_at=workflow_run.finished_at if is_final else None,
            attempt_number=attempt.attempt_number,
        )
        if decision.retry:
            LOG.info(
                "Skipped abandoning stale retry because its terminal side-effect lease is still young",
                workflow_run_id=attempt.workflow_run_id,
                attempt_number=attempt.attempt_number,
            )
            return
        await self._run_terminal_side_effects(workflow_run, decision)

    async def _release_unreleased_terminal_effects(self, attempt: Any) -> None:
        workflow_run = await app.DATABASE.workflow_runs.get_workflow_run(
            workflow_run_id=attempt.workflow_run_id,
            organization_id=attempt.organization_id,
        )
        if workflow_run is None:
            LOG.warning(
                "Cannot recover terminal effects because its workflow run is missing",
                workflow_run_id=attempt.workflow_run_id,
                attempt_number=attempt.attempt_number,
            )
            return

        decision = RetryDecision(
            retry=False,
            attempt_number=attempt.attempt_number,
            delay_seconds=0,
            send_webhook_now=True,
            decision_reason=attempt.decision_reason,
        )
        await self._run_terminal_side_effects(
            workflow_run,
            decision,
            side_effects_claim_at=getattr(attempt, "side_effects_released_at", None),
        )

    async def _run_terminal_side_effects(
        self,
        workflow_run: Any,
        decision: RetryDecision,
        *,
        side_effects_claim_at: Any = None,
    ) -> None:
        try:
            api_key = await self._get_valid_api_key(workflow_run.organization_id)
        except Exception:
            LOG.exception(
                "Failed to load organization API token for terminal recovery",
                organization_id=workflow_run.organization_id,
            )
            api_key = None
        kwargs: dict[str, Any] = {"api_key": api_key} if api_key is not None else {}
        if side_effects_claim_at is not None:
            kwargs["side_effects_claim_at"] = side_effects_claim_at
        run_with_retries = app.WORKFLOW_SERVICE._run_terminal_side_effects_with_retries
        await run_with_retries(workflow_run, decision, **kwargs)

    async def _release_pending_interim_effects(self, attempt: Any, workflow_run: Any, api_key: str) -> bool:
        """Complete a failed interim release before its durable retry attempt is prepared."""
        if not hasattr(attempt, "interim_webhook_sent_at") or attempt.interim_webhook_sent_at is not None:
            return True

        decision = await get_recorded_decision(
            attempt.workflow_run_id,
            attempt_number=attempt.attempt_number,
        )
        if decision is None or not decision.retry:
            LOG.warning(
                "Cannot recover interim workflow webhook without a retry decision",
                workflow_run_id=attempt.workflow_run_id,
                attempt_number=attempt.attempt_number,
            )
            return False

        outcome = await app.WORKFLOW_SERVICE._run_interim_side_effects_with_retries(
            workflow_run,
            decision,
            api_key=api_key,
            side_effects_claim_at=getattr(attempt, "side_effects_released_at", None),
        )
        if outcome not in {"released", "already_released"}:
            LOG.warning(
                "Interim workflow webhook release did not complete; leaving retry unprepared",
                workflow_run_id=attempt.workflow_run_id,
                attempt_number=attempt.attempt_number,
                outcome=outcome,
            )
            return False

        attempts = await app.DATABASE.workflow_run_attempts.get_attempts(attempt.workflow_run_id)
        current_attempt = next((row for row in attempts if row.attempt_number == attempt.attempt_number), None)
        if current_attempt is None or getattr(current_attempt, "interim_webhook_sent_at", None) is None:
            LOG.warning(
                "Interim workflow webhook release returned without its completion marker; leaving retry unprepared",
                workflow_run_id=attempt.workflow_run_id,
                attempt_number=attempt.attempt_number,
            )
            return False
        return True

    async def _get_valid_api_key(self, organization_id: str) -> str | None:
        organizations = getattr(app.DATABASE, "organizations", None)
        get_valid_org_auth_token = getattr(organizations, "get_valid_org_auth_token", None)
        if get_valid_org_auth_token is None:
            return None
        org_auth_token = await get_valid_org_auth_token(
            organization_id=organization_id,
            token_type=OrganizationAuthTokenType.api.value,
        )
        return org_auth_token.token if org_auth_token else None

    async def _initialize_retry_resume(self, attempt: Any, *, prepared: bool) -> tuple[Organization, str] | None:
        deadline = naive_utc_now() + timedelta(seconds=LEASE_TAKEOVER_SECONDS)
        backoff_seconds = 1.0
        previous_missing: str | None = None
        while True:
            missing: str | None = None
            try:
                organization = await app.DATABASE.organizations.get_organization(attempt.organization_id)
                if organization is None:
                    missing = "organization"
                else:
                    api_key = await self._get_valid_api_key(attempt.organization_id)
                    if api_key is None:
                        missing = "API token"
                    else:
                        # Initialization stays ahead of the dispatch claim so a transient failure here
                        # leaves a prepared attempt for the next sweep instead of consuming the retry.
                        await initialize_skyvern_state_file(
                            workflow_run_id=attempt.workflow_run_id,
                            organization_id=attempt.organization_id,
                        )
                        await prepare_org_llm_runtime(app.DATABASE, attempt.organization_id, organization)
                        return organization, api_key
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception(
                    "Failed to initialize workflow retry recovery; will retry",
                    workflow_run_id=attempt.workflow_run_id,
                    attempt_number=attempt.attempt_number,
                )
            if missing is not None and missing == previous_missing:
                LOG.warning(
                    "Cannot resume retry because organization or API token is missing after confirmation",
                    workflow_run_id=attempt.workflow_run_id,
                    attempt_number=attempt.attempt_number,
                    missing=missing,
                )
                if prepared:
                    claimed = await app.DATABASE.workflow_run_attempts.fail_prepared_workflow_run(
                        workflow_run_id=attempt.workflow_run_id,
                        organization_id=attempt.organization_id,
                        attempt_number=attempt.attempt_number,
                        failure_reason=f"Workflow retry cannot resume because its {missing} is unavailable.",
                    )
                    if claimed:
                        await self._abandon_retry(
                            attempt, reason="missing_organization" if missing == "organization" else "missing_api_token"
                        )
                return None
            previous_missing = missing
            remaining_seconds = (deadline - naive_utc_now()).total_seconds()
            if remaining_seconds <= 0:
                self._retry_resumes_needing_recovery.add((attempt.workflow_run_id, attempt.attempt_number))
                LOG.warning(
                    "Workflow retry initialization timed out; leaving recovery to the next sweep",
                    workflow_run_id=attempt.workflow_run_id,
                    attempt_number=attempt.attempt_number,
                )
                return None
            await asyncio.sleep(min(backoff_seconds, remaining_seconds))
            backoff_seconds = min(backoff_seconds * 2, 30.0)

    def _schedule_retry_resume(self, attempt: Any) -> None:
        key = (attempt.workflow_run_id, attempt.attempt_number)
        if key in self._scheduled_retry_resumes:
            return
        self._scheduled_retry_resumes.add(key)
        try:
            task = self._schedule(None, self._resume_pending_retry, attempt)
        except Exception:
            self._scheduled_retry_resumes.discard(key)
            raise
        if task is not None:
            self._retry_resume_tasks.add(task)
            task.add_done_callback(self._retry_resume_tasks.discard)

    async def _resume_pending_retry(self, attempt: Any) -> None:
        key = (attempt.workflow_run_id, attempt.attempt_number)
        claimed_prepared_execution = False
        execution_dispatched = False
        self._retry_resumes_needing_recovery.discard(key)
        try:
            pending_dispatch = self._retry_dispatches_needing_recovery.get(key)
            if pending_dispatch is not None:
                execution_dispatched = True
                await self._dispatch_retry_resume(pending_dispatch[0], pending_dispatch[1])
                return
            prepared = getattr(attempt, "retry_decision", "retry") is None
            if not prepared:
                next_attempt_at = to_naive_utc(attempt.next_attempt_at)
                if next_attempt_at is None:
                    return
                delay_seconds = (next_attempt_at - naive_utc_now()).total_seconds()
                if delay_seconds > 0:
                    await asyncio.sleep(delay_seconds)
            if prepared:
                workflow_run = await app.DATABASE.workflow_runs.get_workflow_run(
                    workflow_run_id=attempt.workflow_run_id,
                    organization_id=attempt.organization_id,
                )
                if (
                    workflow_run is None
                    or workflow_run.status != WorkflowRunStatus.queued
                    or workflow_run.started_at is not None
                ):
                    return
                execution_status = await app.AGENT_FUNCTION.get_workflow_run_execution_status(workflow_run)
                if execution_status not in {"absent", "terminal"}:
                    return

            if getattr(attempt, "decision_reason", None) == "unrecoverable_block_outputs":
                if prepared:
                    claimed = await app.DATABASE.workflow_run_attempts.fail_prepared_workflow_run(
                        workflow_run_id=attempt.workflow_run_id,
                        organization_id=attempt.organization_id,
                        attempt_number=attempt.attempt_number,
                        failure_reason="Workflow retry inputs cannot be restored after a process restart.",
                    )
                    if not claimed:
                        return
                await self._abandon_retry(attempt, reason="unrecoverable_block_outputs")
                return
            initialization = await self._initialize_retry_resume(attempt, prepared=prepared)
            if initialization is None:
                return
            organization, api_key = initialization

            if not prepared and hasattr(attempt, "interim_webhook_sent_at") and attempt.interim_webhook_sent_at is None:
                workflow_run = await app.DATABASE.workflow_runs.get_workflow_run(
                    workflow_run_id=attempt.workflow_run_id,
                    organization_id=attempt.organization_id,
                )
                if workflow_run is None:
                    LOG.warning(
                        "Cannot recover interim workflow webhook because its workflow run is missing",
                        workflow_run_id=attempt.workflow_run_id,
                        attempt_number=attempt.attempt_number,
                    )
                    return
                if not await self._release_pending_interim_effects(attempt, workflow_run, api_key):
                    return

            if prepared:
                claim_stamp = await app.DATABASE.workflow_run_attempts.claim_prepared_attempt_execution(
                    attempt.workflow_run_id, attempt.organization_id, attempt.attempt_number
                )
                if not claim_stamp:
                    return
                attempt.started_at = claim_stamp
                claimed_prepared_execution = True

            if prepared:
                pinned_browser_session_id = attempt.pinned_browser_session_id
                attempt_number = attempt.attempt_number
            else:
                preparation = await prepare_next_attempt_result(
                    workflow_run_id=attempt.workflow_run_id,
                    organization_id=attempt.organization_id,
                    from_attempt=attempt.attempt_number,
                    clear_browser_address=False,
                )
                if preparation.status == "already_prepared":
                    return
                if preparation.status == "failed":
                    await self._abandon_retry(attempt, reason="prepare_cas_failed")
                    return
                pinned_browser_session_id = preparation.pinned_browser_session_id
                attempt_number = attempt.attempt_number + 1

            execution_kwargs: dict[str, Any] = {
                "workflow_run_id": attempt.workflow_run_id,
                "api_key": api_key,
                "organization": organization,
                "browser_session_id": pinned_browser_session_id,
                "block_labels": None,
                "block_outputs": None,
                "need_call_webhook": True,
                "attempt_number": attempt_number,
            }
            if prepared:
                execution_kwargs["prepared_attempt_claimed"] = True
                execution_kwargs["dispatch_claim_started_at"] = attempt.started_at
            execution_dispatched = True
            await self._dispatch_retry_resume(attempt, execution_kwargs)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.exception(
                "Failed to resume pending workflow retry",
                workflow_run_id=attempt.workflow_run_id,
                attempt_number=attempt.attempt_number,
            )
            if claimed_prepared_execution and not execution_dispatched:
                if not await self._retry_dispatch_claim_is_current(attempt):
                    return
                failed_run = await app.WORKFLOW_SERVICE.mark_workflow_run_as_failed_if_not_final(
                    workflow_run_id=attempt.workflow_run_id,
                    failure_reason="Workflow retry recovery could not initialize execution.",
                )
                if failed_run is not None:
                    await self._abandon_retry(attempt, reason="retry_resume_failed")
            elif not execution_dispatched:
                self._retry_resumes_needing_recovery.add(key)
        finally:
            self._scheduled_retry_resumes.discard(key)

    async def _retry_dispatch_claim_is_current(self, attempt: Any) -> bool:
        attempts = await app.DATABASE.workflow_run_attempts.get_attempts(attempt.workflow_run_id)
        current = next((row for row in attempts if row.attempt_number == attempt.attempt_number), None)
        if (
            current is not None
            and current.started_at is not None
            and to_naive_utc(current.started_at) == to_naive_utc(attempt.started_at)
            and current.retry_decision is None
        ):
            return True
        LOG.warning(
            "Workflow retry dispatch claim was replaced; abandoning obsolete dispatch",
            workflow_run_id=attempt.workflow_run_id,
            attempt_number=attempt.attempt_number,
        )
        return False

    async def _dispatch_retry_resume(self, attempt: Any, execution_kwargs: dict[str, Any]) -> None:
        key = (attempt.workflow_run_id, attempt.attempt_number)
        self._retry_dispatches_needing_recovery.pop(key, None)
        execution_started = False

        def on_execution_start() -> None:
            nonlocal execution_started
            execution_started = True

        try:
            if execution_kwargs.get("prepared_attempt_claimed") and not await self._retry_dispatch_claim_is_current(
                attempt
            ):
                self._retry_resumes_needing_recovery.discard(key)
                return
            # Attempt rows let the sweep recover initialization failures; attempt-less runs must fail durably.
            try:
                await initialize_skyvern_state_file(
                    workflow_run_id=attempt.workflow_run_id, organization_id=attempt.organization_id
                )
                await prepare_org_llm_runtime(
                    app.DATABASE, attempt.organization_id, execution_kwargs.get("organization")
                )
            except Exception as exc:
                if await fail_run_without_attempt_row(
                    attempt.workflow_run_id,
                    f"Workflow run initialization failed before execution: {type(exc).__name__}: {exc}",
                    api_key=execution_kwargs.get("api_key"),
                    need_call_webhook=execution_kwargs.get("need_call_webhook", True),
                ):
                    self._retry_resumes_needing_recovery.discard(key)
                    LOG.warning(
                        "Workflow run initialization failed without an attempt row; run is terminal",
                        workflow_run_id=attempt.workflow_run_id,
                        exc_info=True,
                    )
                    return
                raise
            await app.WORKFLOW_SERVICE.execute_workflow_with_retries(
                **execution_kwargs, on_execution_start=on_execution_start
            )
        except Exception:
            if execution_started:
                # The attempt ran, so a durable retry decision can already exist; the sweep resumes it.
                pending_key = await self._pending_retry_key(attempt.workflow_run_id, fallback=key)
                if pending_key is not None:
                    self._retry_resumes_needing_recovery.add(pending_key)
                raise
            self._retry_dispatches_needing_recovery[key] = (attempt, execution_kwargs)
            self._retry_resumes_needing_recovery.add(key)
            LOG.exception(
                "Workflow retry entry failed before execution; leaving recovery to the next sweep",
                workflow_run_id=attempt.workflow_run_id,
                attempt_number=execution_kwargs["attempt_number"],
            )

    async def _pending_retry_key(self, workflow_run_id: str, *, fallback: tuple[str, int]) -> tuple[str, int] | None:
        try:
            attempts = await app.DATABASE.workflow_run_attempts.get_attempts(workflow_run_id)
        except Exception:
            LOG.warning(
                "Failed to read workflow attempts after a retry owner failure; leaving recovery to the next sweep",
                workflow_run_id=workflow_run_id,
                exc_info=True,
            )
            return fallback
        latest = latest_attempt_awaiting_preparation(attempts)
        return None if latest is None else (workflow_run_id, latest.attempt_number)

    def _schedule(
        self,
        background_tasks: BackgroundTasks | None,
        func: Callable[..., Coroutine[Any, Any, Any]],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> asyncio.Task | None:
        if background_tasks:
            background_tasks.add_task(func, *args, **kwargs)
            return None
        task = asyncio.create_task(_run_with_own_context(func, *args, **kwargs))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    async def execute_task(
        self,
        request: Request | None,
        background_tasks: BackgroundTasks | None,
        task_id: str,
        organization_id: str,
        max_steps_override: int | None,
        api_key: str | None,
        browser_session_id: str | None,
        **kwargs: dict,
    ) -> None:
        LOG.info("Executing task using background task executor", task_id=task_id)
        organization = await app.DATABASE.organizations.get_organization(organization_id)
        if organization is None:
            raise OrganizationNotFound(organization_id)

        step = await app.DATABASE.tasks.create_step(
            task_id,
            order=0,
            retry_index=0,
            organization_id=organization_id,
        )

        task = await app.DATABASE.tasks.update_task(
            task_id,
            status=TaskStatus.running,
            organization_id=organization_id,
        )

        close_browser_on_completion = browser_session_id is None and not task.browser_address

        run_obj = await app.DATABASE.tasks.get_run(run_id=task_id, organization_id=organization_id)
        engine = RunEngine.skyvern_v1
        if run_obj and run_obj.task_run_type == RunType.openai_cua:
            engine = RunEngine.openai_cua
        elif run_obj and run_obj.task_run_type == RunType.anthropic_cua:
            engine = RunEngine.anthropic_cua
        elif run_obj and run_obj.task_run_type == RunType.ui_tars:
            engine = RunEngine.ui_tars
        elif run_obj and run_obj.task_run_type == RunType.yutori_navigator:
            engine = RunEngine.yutori_navigator
        elif run_obj and run_obj.task_run_type == RunType.task_v3:
            engine = RunEngine.skyvern_v3

        context: SkyvernContext = skyvern_context.ensure_context()
        context.task_id = task.task_id
        context.run_id = context.run_id or task.task_id
        context.organization_id = organization_id
        context.max_steps_override = max_steps_override
        context.max_screenshot_scrolls = task.max_screenshot_scrolls

        await prepare_org_llm_runtime(app.DATABASE, organization_id, organization)
        await initialize_skyvern_state_file(task_id=task_id, organization_id=organization_id)
        self._schedule(
            background_tasks,
            app.agent.execute_step,
            organization,
            task,
            step,
            api_key,
            close_browser_on_completion=close_browser_on_completion,
            browser_session_id=browser_session_id,
            engine=engine,
        )

    async def execute_workflow(
        self,
        request: Request | None,
        background_tasks: BackgroundTasks | None,
        organization: Organization,
        workflow_id: str,
        workflow_run_id: str,
        workflow_permanent_id: str,
        max_steps_override: int | None,
        api_key: str | None,
        browser_session_id: str | None,
        block_labels: list[str] | None,
        block_outputs: dict[str, Any] | None,
        **kwargs: dict,
    ) -> None:
        LOG.info(
            "Executing workflow using background task executor",
            workflow_run_id=workflow_run_id,
        )

        workflow_run = await app.DATABASE.workflow_runs.get_workflow_run(
            workflow_run_id,
            organization_id=organization.organization_id,
        )
        try:
            attempt_rows = await app.DATABASE.workflow_run_attempts.get_attempts(workflow_run_id)
        except Exception:
            # Attempt lookup only selects the retry-aware attempt number. The owner repeats the
            # lookup, and a dispatch that fails before execution is retried by the recovery sweep.
            LOG.warning(
                "Failed to load workflow retry attempts; scheduling as an attempt-one run",
                workflow_run_id=workflow_run_id,
                exc_info=True,
            )
            attempt_rows = []
        attempt_number = max((row.attempt_number for row in attempt_rows), default=1)
        if block_labels or block_outputs:
            # Scoped inputs live only in this process. The marker must be durable before the queued write
            # below makes the dispatch recoverable, or a restart would run the whole workflow instead.
            await app.DATABASE.workflow_run_attempts.mark_attempt_inputs_unrecoverable(workflow_run_id, attempt_number)
        if workflow_run and workflow_run.sequential_credential_id:
            if workflow_run.browser_session_id:
                persistent_browser_session = await app.DATABASE.browser_sessions.get_persistent_browser_session(
                    session_id=workflow_run.browser_session_id,
                    organization_id=organization.organization_id,
                )
                if (
                    persistent_browser_session
                    and persistent_browser_session.runnable_type == FORCED_WORKFLOW_SESSION_RUNNABLE_TYPE
                ):
                    try:
                        await app.PERSISTENT_SESSIONS_MANAGER.close_session(
                            organization.organization_id,
                            workflow_run.browser_session_id,
                            reason=BrowserSessionCloseReason.aborted,
                        )
                    except Exception:
                        LOG.exception(
                            "Failed to close forced browser session before rejecting sequential credential run",
                            organization_id=organization.organization_id,
                            workflow_run_id=workflow_run_id,
                            browser_session_id=workflow_run.browser_session_id,
                        )
            await app.WORKFLOW_SERVICE.mark_workflow_run_as_failed_if_not_final(
                workflow_run_id=workflow_run_id,
                failure_reason=(
                    "Sequential credential execution is unavailable in the background executor; "
                    "the run failed closed before execution."
                ),
                cascade_children=True,
            )
            if attempt_rows:
                terminal_run = await app.DATABASE.workflow_runs.get_workflow_run(
                    workflow_run_id=workflow_run_id,
                    organization_id=organization.organization_id,
                )
                if terminal_run is not None:
                    decision = await finalize_abandoned_attempt(
                        workflow_run_id=workflow_run_id,
                        organization_id=organization.organization_id,
                        reason="sequential_credential_unsupported",
                        status=terminal_run.status,
                        failure_reason=terminal_run.failure_reason,
                        finished_at=terminal_run.finished_at,
                    )
                    await app.WORKFLOW_SERVICE._run_terminal_side_effects_with_retries(
                        terminal_run,
                        decision,
                        api_key=api_key,
                    )
            else:
                try:
                    terminal_run = await app.DATABASE.workflow_runs.get_workflow_run(
                        workflow_run_id=workflow_run_id,
                        organization_id=organization.organization_id,
                    )
                    if terminal_run is not None:
                        await app.WORKFLOW_SERVICE.execute_workflow_webhook(
                            terminal_run, api_key=api_key, claim_kind=None
                        )
                except Exception:
                    LOG.warning(
                        "Failed to deliver workflow webhook after rejecting a sequential credential run",
                        workflow_run_id=workflow_run_id,
                        exc_info=True,
                    )
            raise BackgroundSequentialCredentialUnsupported(workflow_run_id)

        attempt = next(
            (row for row in attempt_rows if row.attempt_number == attempt_number),
            SimpleNamespace(
                workflow_run_id=workflow_run_id,
                organization_id=organization.organization_id,
                attempt_number=attempt_number,
            ),
        )
        # A dispatch lost with the process is recovered by the prepared-attempt sweep, which admits only a
        # queued run and attempt. The write re-reads the run, so it runs even when the lookups failed.
        if not await queue_initial_attempt(workflow_run_id, attempt_number):
            return
        execution_kwargs: dict[str, Any] = {
            "workflow_run_id": workflow_run_id,
            "api_key": api_key,
            "organization": organization,
            "browser_session_id": browser_session_id,
            "block_labels": block_labels,
            "block_outputs": block_outputs,
            "need_call_webhook": True,
            "attempt_number": attempt_number,
            "claim_initial_attempt": True,
        }
        self._schedule(background_tasks, self._dispatch_retry_resume, attempt, execution_kwargs)

    async def execute_task_v2(
        self,
        request: Request | None,
        background_tasks: BackgroundTasks | None,
        organization_id: str,
        task_v2_id: str,
        max_steps_override: int | str | None,
        browser_session_id: str | None,
        max_iterations_override: int | str | None = None,
        **kwargs: dict,
    ) -> None:
        LOG.info(
            "Executing cruise using background task executor",
            task_v2_id=task_v2_id,
        )

        organization = await app.DATABASE.organizations.get_organization(organization_id)
        if organization is None:
            raise OrganizationNotFound(organization_id)

        task_v2 = await app.DATABASE.observer.get_task_v2(task_v2_id=task_v2_id, organization_id=organization_id)
        if not task_v2 or not task_v2.workflow_run_id:
            raise ValueError("No task v2 or no workflow run associated with task v2")

        # mark task v2 as queued
        await app.DATABASE.observer.update_task_v2(
            task_v2_id=task_v2_id,
            status=TaskV2Status.queued,
            organization_id=organization_id,
        )
        await app.DATABASE.workflow_runs.update_workflow_run(
            workflow_run_id=task_v2.workflow_run_id,
            status=WorkflowRunStatus.queued,
        )

        await initialize_skyvern_state_file(workflow_run_id=task_v2.workflow_run_id, organization_id=organization_id)
        await prepare_org_llm_runtime(app.DATABASE, organization_id, organization)
        self._schedule(
            background_tasks,
            task_v2_service.run_task_v2,
            organization=organization,
            task_v2_id=task_v2_id,
            max_steps_override=max_steps_override,
            max_iterations_override=max_iterations_override,
            browser_session_id=browser_session_id,
        )

    async def execute_script(
        self,
        request: Request | None,
        script_id: str,
        organization_id: str,
        parameters: dict[str, Any] | None = None,
        workflow_run_id: str | None = None,
        background_tasks: BackgroundTasks | None = None,
        **kwargs: dict,
    ) -> None:
        self._schedule(
            background_tasks,
            script_service.execute_script,
            script_id=script_id,
            organization_id=organization_id,
            parameters=parameters,
            workflow_run_id=workflow_run_id,
            background_tasks=background_tasks,
        )
