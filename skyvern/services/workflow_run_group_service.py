from __future__ import annotations

import asyncio
import contextvars
import hmac
import json
import uuid
from collections.abc import Coroutine, Mapping
from datetime import datetime, timedelta
from hashlib import sha256
from http import HTTPStatus
from typing import Any

import structlog
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from skyvern.config import settings
from skyvern.exceptions import SkyvernHTTPException, WorkflowHasNoBlocks
from skyvern.forge import app
from skyvern.forge.sdk.core.permissions.permission_checker_factory import PermissionCheckerFactory
from skyvern.forge.sdk.db.datetime_utils import naive_utc_now, to_naive_utc
from skyvern.forge.sdk.db.enums import WorkflowRunTriggerType
from skyvern.forge.sdk.db.repositories.workflow_run_groups import scrubbed_submission_key
from skyvern.forge.sdk.executor import factory as executor_factory
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.workflow.models.block import (
    BaseTaskBlock,
    HumanInteractionBlock,
    WorkflowTriggerBlock,
    get_all_blocks,
)
from skyvern.forge.sdk.workflow.models.workflow import (
    Workflow,
    WorkflowRequestBody,
    WorkflowRun,
    WorkflowRunStatus,
)
from skyvern.forge.sdk.workflow.retry_policy import resolve_workflow_retry_policy
from skyvern.schemas.workflow_run_groups import (
    IN_FLIGHT_ITEM_STATES,
    WorkflowRunGroup,
    WorkflowRunGroupCreateRequest,
    WorkflowRunGroupItem,
    WorkflowRunGroupItemOutcome,
    WorkflowRunGroupItemResponse,
    WorkflowRunGroupItemState,
    WorkflowRunGroupResponse,
    WorkflowRunGroupStatus,
)
from skyvern.services import run_service, workflow_service
from skyvern.services.organization_log_scope import organization_log_scope

LOG = structlog.get_logger()

DISPATCH_GRACE = timedelta(minutes=10)
MAX_DISPATCH_ATTEMPTS = 3
# ponytail: a fixed settle window, because a group child records no "worker stopped" signal (it has no attempt row).
# It outlasts the longest step that ignores a cancel (a code block); a worker slower than that can still overlap.
CHILD_STOP_SETTLE = timedelta(seconds=settings.CODE_BLOCK_EXECUTION_TIMEOUT_SECONDS + 60)
# An outside writer (a run cancel, the stuck-run reaper) can set these while the child's worker is still running.
_STATUSES_SET_BESIDE_A_LIVE_WORKER = (WorkflowRunStatus.canceled, WorkflowRunStatus.timed_out)
RECOVERY_BATCH_SIZE = 200
RECOVERY_INTERVAL_SECONDS = 60
_FINGERPRINT_DOMAIN = b"skyvern.workflow_run_group_submission.v1\0"
_background_tasks: set[asyncio.Task[None]] = set()


def submission_fingerprint(request: WorkflowRunGroupCreateRequest) -> str:
    payload = {
        "workflow_id": request.workflow_id,
        "version": request.version,
        "items": [{"key": item.key, "parameters": item.parameters} for item in request.items],
    }
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hmac.new(settings.SECRET_KEY.encode(), _FINGERPRINT_DOMAIN + serialized.encode(), sha256).hexdigest()


def non_fresh_browser_reason(workflow: Workflow) -> str | None:
    if resolve_workflow_retry_policy(workflow) is not None:
        return "a retry policy, which could replay a child whose effects already happened"
    if workflow.persist_browser_session:
        return "persist_browser_session"
    if workflow.browser_profile_id or workflow.browser_profile_key:
        return "a browser profile"
    if workflow.sequential_key:
        return "a sequential_key"
    if workflow.workflow_definition.finally_block_label:
        return "a finally block, which runs after the run first reports a final status"
    for block in get_all_blocks(workflow.workflow_definition.blocks):
        if not isinstance(block, WorkflowTriggerBlock):
            continue
        if block.browser_session_id:
            return f"block {block.label} triggering a workflow into a supplied browser_session_id"
        if not block.wait_for_completion:
            return (
                f"block {block.label} triggering a workflow without waiting for it, so that run could outlive its item"
            )
    return None


async def _existing_group_or_conflict(
    group: WorkflowRunGroup, fingerprint: str, organization_id: str
) -> WorkflowRunGroupResponse:
    if not hmac.compare_digest(group.input_fingerprint, fingerprint):
        raise SkyvernHTTPException(
            "submission_key was already used with different input",
            status_code=HTTPStatus.CONFLICT,
        )
    return await get_workflow_run_group(group.workflow_run_group_id, organization_id)


async def submit_workflow_run_group(
    organization: Organization,
    request: WorkflowRunGroupCreateRequest,
    *,
    expected_workflow_modified_at: datetime | None = None,
    expected_latest_groups: tuple[str, Mapping[str, str]] | None = None,
) -> WorkflowRunGroupResponse:
    organization_id = organization.organization_id
    fingerprint = submission_fingerprint(request)
    existing = await app.DATABASE.workflow_run_groups.get_group_by_submission_key(
        organization_id, request.submission_key
    )
    if existing is not None:
        return await _existing_group_or_conflict(existing, fingerprint, organization_id)
    if await app.DATABASE.workflow_run_groups.get_group_by_submission_key(
        organization_id, scrubbed_submission_key(request.submission_key)
    ):
        raise SkyvernHTTPException(
            "submission_key belongs to a group whose records were removed by the organization's retention policy",
            status_code=HTTPStatus.CONFLICT,
        )
    if not settings.WORKFLOW_RUN_GROUPS_SUBMIT_ENABLED:
        raise SkyvernHTTPException(
            "Workflow run group submission is disabled", status_code=HTTPStatus.SERVICE_UNAVAILABLE
        )
    await app.RATE_LIMITER.rate_limit_submit_run(organization_id)

    workflow = await app.WORKFLOW_SERVICE.get_workflow_by_permanent_id(
        workflow_permanent_id=request.workflow_id,
        organization_id=organization_id,
        version=request.version,
    )
    if not workflow.workflow_definition.blocks:
        raise WorkflowHasNoBlocks(workflow_permanent_id=workflow.workflow_permanent_id)
    if reason := non_fresh_browser_reason(workflow):
        raise SkyvernHTTPException(
            f"Workflow version {workflow.version} cannot run as a group because it has {reason}",
            status_code=HTTPStatus.BAD_REQUEST,
        )
    input_parameters = {
        parameter.key: parameter
        for parameter in await app.WORKFLOW_SERVICE.get_workflow_parameters(workflow_id=workflow.workflow_id)
    }
    for item in request.items:
        await app.WORKFLOW_SERVICE.validate_schedule_parameters(workflow, organization, item.parameters)
        # Values are otherwise converted only when a child is created, so a mistyped one would fail mid-group.
        for key, value in item.parameters.items():
            if value is not None and key in input_parameters:
                app.WORKFLOW_SERVICE._serialize_workflow_run_parameter_value(input_parameters[key], value)

    try:
        group = await app.DATABASE.workflow_run_groups.create_group(
            organization_id=organization_id,
            workflow_permanent_id=workflow.workflow_permanent_id,
            requested_version=request.version,
            workflow_id=workflow.workflow_id,
            submission_key=request.submission_key,
            input_fingerprint=fingerprint,
            items=[(item.key, item.parameters) for item in request.items],
            # Without a reviewed timestamp, bind to the row validation read so an edit landing after it is refused.
            expected_workflow_modified_at=expected_workflow_modified_at or workflow.modified_at,
            expected_latest_groups=expected_latest_groups,
        )
    except IntegrityError:
        raced = await app.DATABASE.workflow_run_groups.get_group_by_submission_key(
            organization_id, request.submission_key
        )
        if raced is None:
            raise
        return await _existing_group_or_conflict(raced, fingerprint, organization_id)

    LOG.info(
        "Workflow run group submitted",
        workflow_run_group_id=group.workflow_run_group_id,
        workflow_id=group.workflow_id,
        item_count=len(request.items),
    )
    _spawn(_advance_in_org_scope(group.workflow_run_group_id, organization_id))
    return await get_workflow_run_group(group.workflow_run_group_id, organization_id)


def _is_task_family_only(workflow: Workflow | None) -> bool:
    if workflow is None or not workflow.workflow_definition.blocks:
        return False
    # Task-family blocks act only through recorded steps; a human interaction block sends email without one.
    return all(
        isinstance(block, BaseTaskBlock) and not isinstance(block, HumanInteractionBlock)
        for block in workflow.workflow_definition.blocks
    )


def classify_item_outcome(
    item_state: WorkflowRunGroupItemState,
    child: WorkflowRun | None,
    *,
    task_family_only: bool,
    step_count: int | None,
) -> WorkflowRunGroupItemOutcome:
    if item_state == WorkflowRunGroupItemState.failed_to_start:
        # A child can already have run to a final status before its item was failed; its effects are then unknown.
        if child is not None and child.started_at is not None:
            return WorkflowRunGroupItemOutcome.unknown
        return WorkflowRunGroupItemOutcome.failed
    if item_state == WorkflowRunGroupItemState.pending:
        return WorkflowRunGroupItemOutcome.pending
    if child is None:
        if item_state == WorkflowRunGroupItemState.canceled:
            return WorkflowRunGroupItemOutcome.canceled
        return WorkflowRunGroupItemOutcome.in_progress
    if not child.status.is_final():
        if item_state == WorkflowRunGroupItemState.canceled:
            return WorkflowRunGroupItemOutcome.canceled
        return WorkflowRunGroupItemOutcome.in_progress
    if child.status == WorkflowRunStatus.completed:
        return WorkflowRunGroupItemOutcome.completed
    if child.status == WorkflowRunStatus.canceled and child.started_at is None:
        return WorkflowRunGroupItemOutcome.canceled
    if (
        child.status in (WorkflowRunStatus.failed, WorkflowRunStatus.terminated)
        and task_family_only
        and step_count == 0
        and child.script_run is None
    ):
        return WorkflowRunGroupItemOutcome.failed
    return WorkflowRunGroupItemOutcome.unknown


async def get_workflow_run_group(workflow_run_group_id: str, organization_id: str) -> WorkflowRunGroupResponse:
    group = await app.DATABASE.workflow_run_groups.get_group(workflow_run_group_id, organization_id)
    if group is None:
        raise SkyvernHTTPException(
            f"Workflow run group {workflow_run_group_id} not found", status_code=HTTPStatus.NOT_FOUND
        )
    items = await app.DATABASE.workflow_run_groups.get_items(workflow_run_group_id)
    children = {
        run.workflow_run_id: run
        for run in await app.DATABASE.workflow_runs.get_workflow_runs_by_ids(
            [item.workflow_run_id for item in items], organization_id=organization_id
        )
    }
    task_family_only: bool | None = None
    item_responses = []
    for item in items:
        child = children.get(item.workflow_run_id)
        step_count = None
        if child is not None and child.status in (WorkflowRunStatus.failed, WorkflowRunStatus.terminated):
            if task_family_only is None:
                task_family_only = _is_task_family_only(
                    await app.DATABASE.workflows.get_workflow(group.workflow_id, organization_id)
                )
            if task_family_only:
                step_count = await app.DATABASE.workflow_run_groups.count_steps(item.workflow_run_id)
        item_responses.append(
            WorkflowRunGroupItemResponse(
                key=item.item_key,
                position=item.position,
                workflow_run_id=item.workflow_run_id,
                state=item.state,
                run_status=child.status if child else None,
                outcome=classify_item_outcome(
                    item.state, child, task_family_only=bool(task_family_only), step_count=step_count
                ),
                failure_reason=item.failure_reason or (child.failure_reason if child else None),
            )
        )
    return WorkflowRunGroupResponse(
        workflow_run_group_id=group.workflow_run_group_id,
        workflow_permanent_id=group.workflow_permanent_id,
        workflow_id=group.workflow_id,
        requested_version=group.requested_version,
        submission_key=group.submission_key,
        status=group.status,
        created_at=group.created_at,
        finished_at=group.finished_at,
        items=item_responses,
    )


async def cancel_workflow_run_group(workflow_run_group_id: str, organization_id: str) -> WorkflowRunGroupResponse:
    group = await app.DATABASE.workflow_run_groups.request_cancel(workflow_run_group_id, organization_id)
    if group is None:
        raise SkyvernHTTPException(
            f"Workflow run group {workflow_run_group_id} not found", status_code=HTTPStatus.NOT_FOUND
        )
    await advance_workflow_run_group(workflow_run_group_id)
    return await get_workflow_run_group(workflow_run_group_id, organization_id)


async def _cancel_child(workflow_run_id: str, organization_id: str) -> None:
    try:
        child = await app.DATABASE.workflow_runs.get_workflow_run(workflow_run_id, organization_id=organization_id)
        # On a final run, cancel_workflow_run still resends the webhook, deletes attached files and revokes the attempt.
        if child is None or child.status.is_final():
            return
        await run_service.cancel_workflow_run(workflow_run_id, organization_id)
    except Exception:
        LOG.warning("Failed to cancel workflow run group child", workflow_run_id=workflow_run_id, exc_info=True)


async def _fail_unstarted_child(workflow_run_id: str, failure_reason: str) -> bool:
    """Fail a child that is still `created`, meaning no executor ever took it. False when it moved on or is missing."""
    failed = await app.WORKFLOW_SERVICE.mark_workflow_run_as_failed_if_not_final(
        workflow_run_id=workflow_run_id,
        failure_reason=failure_reason,
        only_from=(WorkflowRunStatus.created,),
    )
    return failed is not None


async def _fail_item_to_start(
    item: WorkflowRunGroupItem,
    dispatch_token: str,
    failure_reason: str,
    organization_id: str,
    from_state: WorkflowRunGroupItemState = WorkflowRunGroupItemState.dispatching,
) -> None:
    # Holding the item in flight first fences out a stale owner whose token was reclaimed, and keeps the next item
    # from starting while an executor that took this child in the meantime may still be running it.
    if not await app.DATABASE.workflow_run_groups.finish_item(
        item.workflow_run_group_id,
        item.position,
        state=WorkflowRunGroupItemState.dispatched,
        from_states=(from_state,),
        dispatch_token=dispatch_token,
    ):
        return
    if not await _fail_unstarted_child(item.workflow_run_id, failure_reason):
        child = await app.DATABASE.workflow_runs.get_workflow_run(item.workflow_run_id, organization_id=organization_id)
        if child is not None and not child.status.is_final():
            # Reconcile releases the item once the canceled child has stopped and settled.
            await _cancel_child(item.workflow_run_id, organization_id)
            return
        if child is not None and _worker_may_still_be_stopping(child):
            return
    # The failed child's terminal hook may already have marked the item done.
    await app.DATABASE.workflow_run_groups.finish_item(
        item.workflow_run_group_id,
        item.position,
        state=WorkflowRunGroupItemState.failed_to_start,
        from_states=(WorkflowRunGroupItemState.dispatched, WorkflowRunGroupItemState.done),
        dispatch_token=dispatch_token,
        failure_reason=failure_reason,
    )


def _failure_reason(message: str, error: Exception) -> str:
    # Exception text can echo item parameters such as credential ids, and this reason is stored and returned.
    return f"{message} ({type(error).__name__})"


async def _dispatch_item(group: WorkflowRunGroup, item: WorkflowRunGroupItem, dispatch_token: str) -> bool:
    """Prepare and execute one claimed item. Returns True while the item stays in flight."""
    # setup_workflow_run replaces the calling task's SkyvernContext and inherits its run ids, so a dispatch that shared
    # its caller's context would hand the previous child's ids to this one.
    return await asyncio.create_task(
        _prepare_and_execute_item_in_org_scope(group, item, dispatch_token), context=contextvars.Context()
    )


async def _prepare_and_execute_item_in_org_scope(
    group: WorkflowRunGroup, item: WorkflowRunGroupItem, dispatch_token: str
) -> bool:
    async with organization_log_scope(group.organization_id):
        return await _prepare_and_execute_item(group, item, dispatch_token)


async def _prepare_and_execute_item(group: WorkflowRunGroup, item: WorkflowRunGroupItem, dispatch_token: str) -> bool:
    organization = await app.DATABASE.organizations.get_organization(group.organization_id)
    if organization is None:
        await _fail_item_to_start(item, dispatch_token, "Organization not found", group.organization_id)
        return False
    try:
        await PermissionCheckerFactory.get_instance().check(organization)
        workflow_run = await workflow_service.prepare_workflow(
            workflow_id=group.workflow_permanent_id,
            organization=organization,
            workflow_request=WorkflowRequestBody(
                data=item.parameters, start_fresh_browser=True, reuse_browser_session=False
            ),
            trigger_type=WorkflowRunTriggerType.api,
            workflow_run_id=item.workflow_run_id,
            resolved_workflow_id=group.workflow_id,
        )
    except IntegrityError:
        # The task_run row is written last, so its presence means an earlier preparation completed.
        if await app.DATABASE.tasks.get_run(item.workflow_run_id, organization_id=group.organization_id) is None:
            await _fail_item_to_start(
                item, dispatch_token, "Workflow run preparation was interrupted", group.organization_id
            )
            return False
        existing_run = await app.DATABASE.workflow_runs.get_workflow_run(
            item.workflow_run_id, organization_id=group.organization_id
        )
        if existing_run is None:
            await _fail_item_to_start(
                item, dispatch_token, "Workflow run preparation was interrupted", group.organization_id
            )
            return False
        workflow_run = existing_run
    except SkyvernHTTPException as e:
        LOG.warning(
            "Workflow run group child could not be prepared",
            workflow_run_group_id=group.workflow_run_group_id,
            workflow_run_id=item.workflow_run_id,
            exc_info=True,
        )
        await _fail_item_to_start(
            item, dispatch_token, _failure_reason("Workflow run could not be prepared", e), group.organization_id
        )
        return False
    except HTTPException as e:
        LOG.warning(
            "Organization cannot start workflow run group child",
            workflow_run_group_id=group.workflow_run_group_id,
            workflow_run_id=item.workflow_run_id,
            exc_info=True,
        )
        await _fail_item_to_start(
            item, dispatch_token, _failure_reason("Organization cannot start runs", e), group.organization_id
        )
        return False
    except Exception:
        LOG.exception(
            "Failed to prepare workflow run group child",
            workflow_run_group_id=group.workflow_run_group_id,
            workflow_run_id=item.workflow_run_id,
            dispatch_attempts=item.dispatch_attempts,
        )
        if item.dispatch_attempts >= MAX_DISPATCH_ATTEMPTS:
            await _fail_item_to_start(
                item, dispatch_token, "Workflow run preparation kept failing", group.organization_id
            )
            return False
        return True

    if workflow_run.status.is_final():
        await app.DATABASE.workflow_run_groups.finish_item(
            group.workflow_run_group_id,
            item.position,
            state=WorkflowRunGroupItemState.done,
            from_states=(WorkflowRunGroupItemState.dispatching,),
            dispatch_token=dispatch_token,
        )
        return False

    flip = await app.DATABASE.workflow_run_groups.flip_to_dispatched(
        group.workflow_run_group_id, item.position, dispatch_token
    )
    if flip == "lost":
        return True
    if flip == "canceled":
        await _cancel_child(item.workflow_run_id, group.organization_id)
        return False

    LOG.info(
        "Dispatching workflow run group child",
        workflow_run_group_id=group.workflow_run_group_id,
        workflow_run_id=item.workflow_run_id,
        position=item.position,
    )
    try:
        await executor_factory.AsyncExecutorFactory.get_executor().execute_workflow(
            request=None,
            background_tasks=None,
            organization=organization,
            workflow_id=workflow_run.workflow_id,
            workflow_run_id=workflow_run.workflow_run_id,
            workflow_permanent_id=workflow_run.workflow_permanent_id,
            max_steps_override=None,
            api_key=None,
            browser_session_id=workflow_run.browser_session_id,
            block_labels=None,
            block_outputs=None,
        )
    except Exception:
        LOG.exception(
            "Failed to execute workflow run group child",
            workflow_run_group_id=group.workflow_run_group_id,
            workflow_run_id=item.workflow_run_id,
        )
    return True


def _worker_may_still_be_stopping(child: WorkflowRun) -> bool:
    if child.status not in _STATUSES_SET_BESIDE_A_LIVE_WORKER:
        return False
    # The reaper's bulk write sets the status without finished_at, so fall back to the row's last write.
    ended_at = to_naive_utc(child.finished_at or child.modified_at)
    return ended_at is None or ended_at > naive_utc_now() - CHILD_STOP_SETTLE


async def _reconcile_in_flight(group: WorkflowRunGroup, items: list[WorkflowRunGroupItem]) -> None:
    in_flight = [item for item in items if item.state in IN_FLIGHT_ITEM_STATES]
    canceling = group.status != WorkflowRunGroupStatus.active
    if canceling:
        in_flight += [item for item in items if item.state == WorkflowRunGroupItemState.canceled]
    if not in_flight:
        return
    children = {
        run.workflow_run_id: run
        for run in await app.DATABASE.workflow_runs.get_workflow_runs_by_ids(
            [item.workflow_run_id for item in in_flight], organization_id=group.organization_id
        )
    }
    stale_before = naive_utc_now() - DISPATCH_GRACE
    for item in in_flight:
        child = children.get(item.workflow_run_id)
        claimed_at = to_naive_utc(item.claimed_at)
        is_stale = claimed_at is not None and claimed_at < stale_before
        if child is not None and not child.status.is_final() and canceling:
            await _cancel_child(item.workflow_run_id, group.organization_id)
            child = await app.DATABASE.workflow_runs.get_workflow_run(item.workflow_run_id)
        if item.state == WorkflowRunGroupItemState.canceled:
            continue
        if child is not None and child.status.is_final():
            # A canceling group starts nothing after this item, so only an active one has to wait out the settle.
            if not canceling and _worker_may_still_be_stopping(child):
                continue
            await app.DATABASE.workflow_run_groups.finish_item(
                group.workflow_run_group_id,
                item.position,
                state=WorkflowRunGroupItemState.done,
                from_states=IN_FLIGHT_ITEM_STATES,
            )
            continue
        if not is_stale:
            continue
        if item.state == WorkflowRunGroupItemState.dispatched:
            # Only a child no executor ever took is failed; a queued or running one is left to the stuck-run reaper.
            # A dispatched item with no child row was held by a failure that stopped before releasing it.
            unstarted = child is None or child.status == WorkflowRunStatus.created
            if unstarted and item.dispatch_token is not None:
                await _fail_item_to_start(
                    item,
                    item.dispatch_token,
                    "Workflow run dispatch was interrupted",
                    group.organization_id,
                    from_state=WorkflowRunGroupItemState.dispatched,
                )
            continue
        dispatch_token = uuid.uuid4().hex
        reclaimed = await app.DATABASE.workflow_run_groups.reclaim_stale_item(
            group.workflow_run_group_id,
            item.position,
            dispatch_token=dispatch_token,
            stale_before=stale_before,
        )
        if reclaimed is None:
            continue
        if reclaimed.dispatch_attempts > MAX_DISPATCH_ATTEMPTS:
            await _fail_item_to_start(
                reclaimed, dispatch_token, "Workflow run dispatch kept stalling", group.organization_id
            )
            continue
        if canceling:
            await _cancel_child(item.workflow_run_id, group.organization_id)
            await app.DATABASE.workflow_run_groups.finish_item(
                group.workflow_run_group_id,
                item.position,
                state=WorkflowRunGroupItemState.canceled,
                from_states=(WorkflowRunGroupItemState.dispatching,),
                dispatch_token=dispatch_token,
            )
            continue
        await _dispatch_item(group, reclaimed, dispatch_token)


async def advance_workflow_run_group(workflow_run_group_id: str) -> None:
    group = await app.DATABASE.workflow_run_groups.get_group(workflow_run_group_id)
    if group is None or group.status == WorkflowRunGroupStatus.finished:
        return
    await _reconcile_in_flight(group, await app.DATABASE.workflow_run_groups.get_items(workflow_run_group_id))
    while True:
        dispatch_token = uuid.uuid4().hex
        item = await app.DATABASE.workflow_run_groups.claim_next_item(workflow_run_group_id, dispatch_token)
        if item is None or await _dispatch_item(group, item, dispatch_token):
            break
    await app.DATABASE.workflow_run_groups.finish_group_if_complete(workflow_run_group_id)


async def _advance_in_org_scope(workflow_run_group_id: str, organization_id: str) -> None:
    async with organization_log_scope(organization_id):
        try:
            await advance_workflow_run_group(workflow_run_group_id)
        except Exception:
            LOG.exception("Failed to advance workflow run group", workflow_run_group_id=workflow_run_group_id)


async def _advance_after_child_terminal(workflow_run_id: str, organization_id: str) -> None:
    try:
        item = await app.DATABASE.workflow_run_groups.get_item_by_workflow_run_id(workflow_run_id)
    except Exception:
        LOG.warning("Failed to look up workflow run group item", workflow_run_id=workflow_run_id, exc_info=True)
        return
    if item is not None:
        await _advance_in_org_scope(item.workflow_run_group_id, organization_id)


def _spawn(coroutine: Coroutine[Any, Any, None]) -> None:
    # An empty context keeps the finishing run's SkyvernContext out of the next child's preparation.
    task = asyncio.create_task(coroutine, context=contextvars.Context())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def schedule_advance_after_terminal(workflow_run: WorkflowRun) -> None:
    if not workflow_run.start_fresh_browser or workflow_run.parent_workflow_run_id is not None:
        return
    _spawn(_advance_after_child_terminal(workflow_run.workflow_run_id, workflow_run.organization_id))


async def is_workflow_run_group_child(workflow_run: WorkflowRun) -> bool:
    if workflow_run.start_fresh_browser is not True:
        return False
    try:
        item = await app.DATABASE.workflow_run_groups.get_item_by_workflow_run_id(workflow_run.workflow_run_id)
    except Exception:
        LOG.warning(
            "Failed to look up workflow run group item", workflow_run_id=workflow_run.workflow_run_id, exc_info=True
        )
        # Fail closed: a group child must not reuse a rotated browser, even though this moves a non-group run off its
        # configured browser addresses for this dispatch.
        return True
    return item is not None


async def recover_workflow_run_groups() -> None:
    groups = await app.DATABASE.workflow_run_groups.list_unfinished_groups(RECOVERY_BATCH_SIZE)
    for workflow_run_group_id, organization_id in groups:
        await _advance_in_org_scope(workflow_run_group_id, organization_id)


async def run_workflow_run_group_recovery_loop() -> None:
    while True:
        try:
            await recover_workflow_run_groups()
        except Exception:
            LOG.exception("Workflow run group recovery sweep failed")
        await asyncio.sleep(RECOVERY_INTERVAL_SECONDS)
