from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from skyvern.forge import app
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.schemas.workflows import Workflow
from skyvern.forge.sdk.workflow.scheduler import workflow_scheduler
from skyvern.forge.sdk.services import org_auth_service

# Create router for workflow scheduler endpoints
router = APIRouter(prefix="/workflows/scheduler", tags=["workflow_scheduler"])


class CronScheduleRequest(BaseModel):
    """Request model for updating a workflow's cron schedule."""
    cron_expression: Optional[str] = None
    timezone: Optional[str] = None
    cron_enabled: Optional[bool] = None


class CronScheduleResponse(BaseModel):
    """Response model for a workflow's cron schedule."""
    workflow_id: str
    cron_expression: Optional[str] = None
    timezone: Optional[str] = None
    cron_enabled: bool
    next_run_time: Optional[datetime] = None


@router.get("/{workflow_id}", response_model=CronScheduleResponse)
async def get_workflow_schedule(workflow_id: str, organization: Organization = Depends(org_auth_service.get_current_org)):
    """Get the cron schedule for a workflow."""
    workflow = await app.DATABASE.get_workflow(workflow_id)
    if not workflow:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workflow {workflow_id} not found",
        )
    
    # Check if the workflow belongs to the organization
    if workflow.organization_id != organization.organization_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to access this workflow",
        )
    
    return CronScheduleResponse(
        workflow_id=workflow.workflow_id,
        cron_expression=workflow.cron_expression,
        timezone=workflow.timezone,
        cron_enabled=workflow.cron_enabled or False,
        next_run_time=workflow.next_run_time,
    )


@router.put("/{workflow_id}", response_model=CronScheduleResponse)
async def update_workflow_schedule(
    workflow_id: str, 
    schedule: CronScheduleRequest, 
    organization: Organization = Depends(org_auth_service.get_current_org)
):
    """Update the cron schedule for a workflow."""
    workflow = await app.DATABASE.get_workflow(workflow_id)
    if not workflow:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workflow {workflow_id} not found",
        )
    
    # Check if the workflow belongs to the organization
    if workflow.organization_id != organization.organization_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to access this workflow",
        )
    
    # Update the workflow's cron schedule
    updated_workflow = await app.DATABASE.update_workflow_cron(
        workflow_id=workflow_id,
        cron_expression=schedule.cron_expression,
        timezone=schedule.timezone,
        cron_enabled=schedule.cron_enabled,
    )
    
    # If the cron schedule was updated, update the scheduler
    if schedule.cron_enabled is not None or schedule.cron_expression is not None or schedule.timezone is not None:
        await workflow_scheduler.update_workflow_schedule(workflow_id)
    
    return CronScheduleResponse(
        workflow_id=updated_workflow.workflow_id,
        cron_expression=updated_workflow.cron_expression,
        timezone=updated_workflow.timezone,
        cron_enabled=updated_workflow.cron_enabled or False,
        next_run_time=updated_workflow.next_run_time,
    )


@router.post("/{workflow_id}/trigger", response_model=dict)
async def trigger_workflow(
    workflow_id: str, 
    organization: Organization = Depends(org_auth_service.get_current_org)
):
    """Manually trigger a workflow run."""
    workflow = await app.DATABASE.get_workflow(workflow_id)
    if not workflow:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workflow {workflow_id} not found",
        )
    
    # Check if the workflow belongs to the organization
    if workflow.organization_id != organization.organization_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to access this workflow",
        )
    
    # Create a workflow run
    workflow_run = await app.DATABASE.create_workflow_run_from_cron(
        workflow_id=workflow_id,
        workflow_permanent_id=workflow.workflow_permanent_id,
        organization_id=organization.organization_id,
        status="created",
        proxy_location=workflow.proxy_location,
        webhook_callback_url=workflow.webhook_callback_url,
        totp_verification_url=workflow.totp_verification_url,
        totp_identifier=workflow.totp_identifier,
    )
    
    # Execute the workflow
    await app.WORKFLOW_SERVICE.execute_workflow(
        workflow_run_id=workflow_run.workflow_run_id,
        api_key=None,
        organization=organization,
        browser_session_id=None,
    )
    
    return {"message": "Workflow triggered successfully", "workflow_run_id": workflow_run.workflow_run_id}
