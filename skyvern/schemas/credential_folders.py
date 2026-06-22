from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CredentialFolderBase(BaseModel):
    title: str = Field(..., description="Folder title", min_length=1, max_length=255)
    description: str | None = Field(None, description="Folder description")


class CredentialFolderCreate(CredentialFolderBase):
    """Request model for creating a credential folder"""


class CredentialFolderUpdate(BaseModel):
    """Request model for updating a credential folder"""

    title: str | None = Field(None, description="Folder title", min_length=1, max_length=255)
    description: str | None = Field(None, description="Folder description")

    @model_validator(mode="after")
    def validate_at_least_one_field(self) -> "CredentialFolderUpdate":
        if self.title is None and self.description is None:
            raise ValueError("at least one of 'title' or 'description' must be provided")
        return self


class CredentialFolder(CredentialFolderBase):
    """Response model for a credential folder"""

    model_config = ConfigDict(from_attributes=True)

    folder_id: str
    organization_id: str
    credential_count: int = Field(0, description="Number of credentials in this folder")
    created_at: datetime
    modified_at: datetime


class UpdateCredentialFolderRequest(BaseModel):
    """Request model for updating a credential's folder assignment"""

    folder_id: str | None = Field(
        ...,
        description="Folder ID to assign credential to. Set explicitly to null to remove from folder.",
    )
