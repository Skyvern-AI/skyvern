from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class FileEncoding(StrEnum):
    """Supported file content encodings."""

    BASE64 = "base64"


class ProjectFile(BaseModel):
    """Model representing a file in a project."""

    path: str = Field(..., description="File path relative to project root", examples=["src/main.py"])
    content: str = Field(..., description="Base64 encoded file content")
    encoding: FileEncoding = Field(default=FileEncoding.BASE64, description="Content encoding")
    mime_type: str | None = Field(default=None, description="MIME type (auto-detected if not provided)")


class CreateProjectRequest(BaseModel):
    workflow_id: str | None = Field(default=None, description="Associated workflow ID")
    run_id: str | None = Field(default=None, description="Associated run ID")
    files: list[ProjectFile] | None = Field(
        default=None,
        description="Array of files to include in the project",
        examples=[
            {
                "path": "main.py",
                "content": "cHJpbnQoIkhlbGxvLCBXb3JsZCEiKQ==",  # base64 encoded "print('Hello, World!')"
                "encoding": "base64",
                "mime_type": "text/x-python",
            },
            {
                "path": "requirements.txt",
                "content": "cmVxdWVzdHM9PTIuMjguMQ==",  # base64 encoded "requests==2.28.1"
                "encoding": "base64",
                "mime_type": "text/plain",
            },
        ],
    )


class FileNode(BaseModel):
    """Model representing a file or directory in the file tree."""

    type: str = Field(..., description="Type of node: 'file' or 'directory'")
    size: int | None = Field(default=None, description="File size in bytes")
    mime_type: str | None = Field(default=None, description="MIME type of the file")
    content_hash: str | None = Field(default=None, description="SHA256 hash of file content")
    created_at: datetime = Field(..., description="Timestamp when the file was created")
    children: dict[str, FileNode] | None = Field(default=None, description="Child nodes for directories")


class DeployProjectRequest(BaseModel):
    """Request model for deploying a project with updated files."""

    files: list[ProjectFile] = Field(
        ...,
        description="Array of files to include in the project",
        examples=[
            {
                "path": "src/main.py",
                "content": "cHJpbnQoIkhlbGxvLCBXb3JsZCEiKQ==",  # base64 encoded "print('Hello, World!')"
                "encoding": "base64",
                "mime_type": "text/x-python",
            }
        ],
    )


class CreateProjectResponse(BaseModel):
    project_id: str = Field(..., description="Unique project identifier", examples=["proj_abc123"])
    version: int = Field(..., description="Project version number", examples=[1])
    workflow_id: str | None = Field(default=None, description="ID of the workflow this project is associated with")
    run_id: str | None = Field(
        default=None, description="ID of the workflow run or task run that generated this project"
    )
    file_count: int = Field(..., description="Total number of files in the project")
    file_tree: dict[str, FileNode] = Field(..., description="Hierarchical file tree structure")
    created_at: datetime = Field(..., description="Timestamp when the project was created")


class Project(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    project_revision_id: str = Field(description="Unique identifier for this specific project revision")
    project_id: str = Field(description="User-facing project identifier, consistent across versions")
    organization_id: str = Field(description="ID of the organization that owns this project")
    workflow_id: str | None = Field(default=None, description="ID of the workflow this project is associated with")
    run_id: str | None = Field(
        default=None, description="ID of the workflow run or task run that generated this project"
    )
    version: int = Field(description="Version number of the project")
    created_at: datetime = Field(description="Timestamp when the project was created")
    modified_at: datetime = Field(description="Timestamp when the project was last modified")
    deleted_at: datetime | None = Field(default=None, description="Timestamp when the project was soft deleted")
