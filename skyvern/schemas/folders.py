from datetime import datetime

from pydantic import BaseModel, Field


class FolderBase(BaseModel):
    title: str = Field(..., description="Folder title", min_length=1, max_length=255)
    description: str | None = Field(None, description="Folder description")


class FolderCreate(FolderBase):
    """Request model for creating a folder"""


class FolderUpdate(BaseModel):
    """Request model for updating a folder"""

    title: str | None = Field(None, description="Folder title", min_length=1, max_length=255)
    description: str | None = Field(None, description="Folder description")


class Folder(FolderBase):
    """Response model for a folder"""

    folder_id: str
    organization_id: str
    workflow_count: int = Field(0, description="Number of workflows in this folder")
    created_at: datetime
    modified_at: datetime

    class Config:
        from_attributes = True


class UpdateWorkflowFolderRequest(BaseModel):
    """Request model for updating a workflow's folder assignment"""

    folder_id: str | None = Field(
        None,
        description="Folder ID to assign workflow to. Set to null to remove from folder.",
    )
