import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import structlog

from skyvern.forge import app
from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
from skyvern.forge.sdk.schemas.task_v2 import TaskV2Status
from skyvern.forge.sdk.schemas.tasks import TaskStatus
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus
from skyvern.services import task_v2_service
from skyvern.utils.deferred_cleanup import finish_cleanup_deferring_cancellation

LOG = structlog.get_logger()

STRICT_OTP_DISPATCH_FAILURE_REASON = "Strict OTP execution was refused before browser acquisition."
STRICT_OTP_PREPARATION_FAILURE_REASON = "Strict OTP execution failed before it could be dispatched."


async def fail_strict_task_dispatch(
    *,
    task_id: str,
    organization_id: str,
    failure_reason: str = STRICT_OTP_DISPATCH_FAILURE_REASON,
    only_if_unclaimed: bool = False,
) -> bool:
    if only_if_unclaimed:
        task = await app.DATABASE.tasks.fail_task_for_temporal_dispatch_precommit(
            task_id,
            organization_id,
            failure_reason,
        )
        finish_claimed = task is not None
        if task is None:
            return False
    else:
        task, finish_claimed = await app.DATABASE.tasks.update_task_and_claim_finish(
            task_id,
            organization_id=organization_id,
            status=TaskStatus.failed,
            failure_reason=failure_reason,
            keep_final_status=True,
        )
    if finish_claimed:
        await app.DATABASE.tasks.sync_task_run_status(
            organization_id=organization_id,
            run_id=task_id,
            status=task.status.value,
            started_at=task.started_at,
            finished_at=task.finished_at,
        )
        try:
            api_key = await app.DATABASE.organizations.get_valid_org_auth_token(
                organization_id,
                OrganizationAuthTokenType.api.value,
            )
            if api_key is not None:
                await app.agent.execute_task_webhook(task=task, api_key=api_key.token)
        except Exception:
            LOG.warning(
                "Failed to send strict task failure webhook",
                task_id=task_id,
                organization_id=organization_id,
                exc_info=True,
            )
    return finish_claimed


async def fail_strict_workflow_dispatch(
    *,
    workflow_run_id: str,
    failure_reason: str = STRICT_OTP_DISPATCH_FAILURE_REASON,
    only_if_unclaimed: bool = False,
) -> bool:
    workflow_run = await app.WORKFLOW_SERVICE.mark_workflow_run_as_failed_if_not_final(
        workflow_run_id=workflow_run_id,
        failure_reason=failure_reason,
        cascade_children=True,
        only_from=[WorkflowRunStatus.running] if only_if_unclaimed else None,
        require_job_id_unset=only_if_unclaimed,
    )
    if workflow_run is None:
        return False
    try:
        api_key = await app.DATABASE.organizations.get_valid_org_auth_token(
            workflow_run.organization_id,
            OrganizationAuthTokenType.api.value,
        )
        await app.WORKFLOW_SERVICE.execute_workflow_webhook(
            workflow_run,
            api_key.token if api_key is not None else None,
            claim_kind=None,
            finalizer="failed_before_execution",
        )
    except Exception:
        LOG.warning(
            "Failed to send strict workflow failure webhook",
            workflow_run_id=workflow_run_id,
            exc_info=True,
        )
    return True


async def fail_strict_task_v2_dispatch(
    *,
    task_v2_id: str,
    organization_id: str,
    workflow_run_id: str,
    failure_reason: str = STRICT_OTP_DISPATCH_FAILURE_REASON,
    only_if_unclaimed: bool = False,
) -> bool:
    workflow_failed = await fail_strict_workflow_dispatch(
        workflow_run_id=workflow_run_id,
        failure_reason=failure_reason,
        only_if_unclaimed=only_if_unclaimed,
    )
    if only_if_unclaimed and not workflow_failed:
        return False
    task_finished = await app.DATABASE.observer.update_task_v2_status_if_not_final(
        task_v2_id=task_v2_id,
        organization_id=organization_id,
        status=TaskV2Status.failed,
    )
    if not task_finished:
        return False
    try:
        task_v2 = await app.DATABASE.observer.get_task_v2(
            task_v2_id=task_v2_id,
            organization_id=organization_id,
        )
        if task_v2 is not None:
            await task_v2_service.send_task_v2_webhook(task_v2)
    except Exception:
        LOG.warning(
            "Failed to send strict task v2 failure webhook",
            task_v2_id=task_v2_id,
            organization_id=organization_id,
            exc_info=True,
        )
    return True


async def fail_unclaimed_strict_workflow_run(workflow_run_id: str) -> None:
    await finish_cleanup_deferring_cancellation(
        lambda: fail_strict_workflow_dispatch(
            workflow_run_id=workflow_run_id,
            failure_reason=STRICT_OTP_PREPARATION_FAILURE_REASON,
            only_if_unclaimed=True,
        ),
        workflow_run_id=workflow_run_id,
    )


async def fail_unclaimed_strict_task_v2(*, task_v2_id: str, organization_id: str, workflow_run_id: str) -> None:
    await finish_cleanup_deferring_cancellation(
        lambda: fail_strict_task_v2_dispatch(
            task_v2_id=task_v2_id,
            organization_id=organization_id,
            workflow_run_id=workflow_run_id,
            failure_reason=STRICT_OTP_PREPARATION_FAILURE_REASON,
            only_if_unclaimed=True,
        ),
        task_v2_id=task_v2_id,
        workflow_run_id=workflow_run_id,
    )


async def fail_unclaimed_strict_task(*, task_id: str, organization_id: str) -> None:
    await finish_cleanup_deferring_cancellation(
        lambda: fail_strict_task_dispatch(
            task_id=task_id,
            organization_id=organization_id,
            failure_reason=STRICT_OTP_PREPARATION_FAILURE_REASON,
            only_if_unclaimed=True,
        ),
        task_id=task_id,
    )


@dataclass
class StrictWorkflowRunCleanup:
    workflow_run_id: str | None = None


@asynccontextmanager
async def fail_unclaimed_strict_workflow_run_on_error() -> AsyncIterator[StrictWorkflowRunCleanup]:
    """Fails the strict run named on the yielded target if the block raises or is cancelled after naming it."""
    target = StrictWorkflowRunCleanup()
    try:
        yield target
    except (Exception, asyncio.CancelledError):
        if target.workflow_run_id is not None:
            await fail_unclaimed_strict_workflow_run(target.workflow_run_id)
        raise
