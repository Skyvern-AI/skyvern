import structlog
from fastapi import BackgroundTasks, Depends, HTTPException, Query, status

from skyvern.forge import app
from skyvern.forge.sdk.routes.routers import base_router
from skyvern.forge.sdk.schemas.feedback import FeedbackEvent, RunFeedback, RunFeedbackRequest, RunFeedbackTargetType
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.services import org_auth_service

LOG = structlog.get_logger()


async def _resolve_target_context(
    target_type: RunFeedbackTargetType, target_id: str, organization_id: str
) -> str | None:
    """Return the target's parent id, or 404 when the target is not visible to the organization."""
    if target_type == "workflow_run":
        workflow_run = await app.DATABASE.workflow_runs.get_workflow_run(
            workflow_run_id=target_id, organization_id=organization_id
        )
        if workflow_run is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
        return workflow_run.workflow_permanent_id
    task = await app.DATABASE.tasks.get_task(target_id, organization_id=organization_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    return task.workflow_run_id


def _is_unchanged(previous: RunFeedback | None, request: RunFeedbackRequest) -> bool:
    if previous is None:
        return request.rating is None
    return (previous.rating, previous.reason, previous.needs_support) == (
        request.rating,
        request.reason or None,
        request.needs_support,
    )


async def _fan_out(organization: Organization, event: FeedbackEvent) -> None:
    try:
        await app.AGENT_FUNCTION.on_feedback_submitted(organization=organization, event=event)
    except Exception:
        LOG.exception(
            "Feedback fan-out failed",
            organization_id=organization.organization_id,
            target_type=event.target_type,
            target_id=event.target_id,
        )


@base_router.post("/feedback", response_model=RunFeedback | None, include_in_schema=False)
async def submit_run_feedback(
    request: RunFeedbackRequest,
    background_tasks: BackgroundTasks,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> RunFeedback | None:
    organization_id = current_org.organization_id
    context_id = await _resolve_target_context(request.target_type, request.target_id, organization_id)
    previous = await app.DATABASE.run_feedback.get_run_feedback(
        organization_id=organization_id, target_type=request.target_type, target_id=request.target_id
    )

    if request.rating is None:
        await app.DATABASE.run_feedback.delete_run_feedback(
            organization_id=organization_id, target_type=request.target_type, target_id=request.target_id
        )
        feedback = None
    else:
        feedback = await app.DATABASE.run_feedback.upsert_run_feedback(
            organization_id=organization_id,
            target_type=request.target_type,
            target_id=request.target_id,
            context_id=context_id,
            rating=request.rating,
            reason=request.reason,
            needs_support=request.needs_support,
            submitted_by=request.submitted_by,
        )

    # A re-save of the same state (a retried request, a double click) is not a new signal for sinks.
    if _is_unchanged(previous, request):
        return feedback

    event = FeedbackEvent(
        target_type=request.target_type,
        target_id=request.target_id,
        context_id=context_id,
        rating=request.rating,
        previous_rating=previous.rating if previous else None,
        reason=request.reason,
        needs_support=request.needs_support,
        submitted_by=request.submitted_by,
    )
    # Sinks (Slack, analytics) run after the response so a slow vendor cannot hold the request open.
    background_tasks.add_task(_fan_out, current_org, event)
    return feedback


@base_router.get("/feedback", response_model=RunFeedback | None, include_in_schema=False)
async def get_run_feedback(
    target_type: RunFeedbackTargetType = Query(...),
    target_id: str = Query(..., min_length=1, max_length=128),
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> RunFeedback | None:
    return await app.DATABASE.run_feedback.get_run_feedback(
        organization_id=current_org.organization_id, target_type=target_type, target_id=target_id
    )
