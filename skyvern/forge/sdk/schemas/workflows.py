from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field


class WorkflowBase(BaseModel):
    """Base model for workflow schemas."""
    name: str
    description: Optional[str] = None
    definition: dict
    organization_id: Optional[str] = None
    cron_expression: Optional[str] = None
    timezone: Optional[str] = None
    cron_enabled: Optional[bool] = False
    next_run_time: Optional[datetime] = None


class Workflow(WorkflowBase):
    """Schema for workflow model representation."""
    id: str
    created_at: datetime
    updated_at: Optional[datetime] = None
    
    class Config:
        from_attributes = True


class WorkflowCreate(WorkflowBase):
    """Schema for creating a new workflow."""
    pass


class WorkflowUpdate(BaseModel):
    """Schema for updating an existing workflow."""
    name: Optional[str] = None
    description: Optional[str] = None
    definition: Optional[dict] = None
    organization_id: Optional[str] = None
    cron_expression: Optional[str] = None
    timezone: Optional[str] = None
    cron_enabled: Optional[bool] = None
    next_run_time: Optional[datetime] = None


class WorkflowResponse(WorkflowBase):
    """Schema for workflow response."""
    id: str
    created_at: datetime
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class WorkflowsResponse(BaseModel):
    """Schema for multiple workflows response."""
    workflows: List[WorkflowResponse]


class WorkflowScheduleUpdate(BaseModel):
    """Schema for updating a workflow's schedule."""
    cron_expression: Optional[str] = None
    timezone: Optional[str] = None
    cron_enabled: Optional[bool] = None
