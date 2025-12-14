from __future__ import annotations

import base64
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class FileEncoding(StrEnum):
    """Supported file content encodings."""

    BASE64 = "base64"
    UTF8 = "utf-8"


class ScriptFile(BaseModel):
    file_id: str
    script_revision_id: str
    script_id: str
    organization_id: str

    file_path: str  # e.g., "src/utils.py"
    file_name: str  # e.g., "utils.py"
    file_type: str  # "file" or "directory"

    # File content and metadata
    content_hash: str | None = None  # SHA-256 hash for deduplication
    file_size: int | None = None  # Size in bytes
    mime_type: str | None = None  # e.g., "text/x-python"
    encoding: FileEncoding | None = Field(default=None, description="Content encoding")

    artifact_id: str | None = None
    created_at: datetime
    modified_at: datetime

    async def get_content(self) -> str:
        # get the content from the artifact
        if self.encoding == FileEncoding.BASE64:
            return base64.b64decode(self.content).decode("utf-8")
        return self.content


class ScriptFileCreate(BaseModel):
    """Model representing a file in a script."""

    path: str = Field(..., description="File path relative to script root", examples=["src/main.py"])
    content: str = Field(..., description="Base64 encoded file content")
    encoding: FileEncoding = Field(default=FileEncoding.BASE64, description="Content encoding")
    mime_type: str | None = Field(default=None, description="MIME type (auto-detected if not provided)")


class CreateScriptRequest(BaseModel):
    workflow_id: str | None = Field(default=None, description="Associated workflow ID")
    run_id: str | None = Field(default=None, description="Associated run ID")
    files: list[ScriptFileCreate] | None = Field(
        default=None,
        description="Array of files to include in the script",
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


class DeployScriptRequest(BaseModel):
    """Request model for deploying a script with updated files."""

    files: list[ScriptFileCreate] = Field(
        ...,
        description="Array of files to include in the script",
        examples=[
            {
                "path": "src/main.py",
                "content": "cHJpbnQoIkhlbGxvLCBXb3JsZCEiKQ==",  # base64 encoded "print('Hello, World!')"
                "encoding": "base64",
                "mime_type": "text/x-python",
            }
        ],
    )


class CreateScriptResponse(BaseModel):
    script_id: str = Field(..., description="Unique script identifier", examples=["s_abc123"])
    version: int = Field(..., description="Script version number", examples=[1])
    run_id: str | None = Field(
        default=None, description="ID of the workflow run or task run that generated this script"
    )
    file_count: int = Field(..., description="Total number of files in the script")
    file_tree: dict[str, FileNode] = Field(..., description="Hierarchical file tree structure")
    created_at: datetime = Field(..., description="Timestamp when the script was created")


class Script(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    script_revision_id: str = Field(description="Unique identifier for this specific script revision")
    script_id: str = Field(description="User-facing script identifier, consistent across versions")
    organization_id: str = Field(description="ID of the organization that owns this script")
    run_id: str | None = Field(
        default=None, description="ID of the workflow run or task run that generated this script"
    )
    version: int = Field(description="Version number of the script")
    created_at: datetime = Field(description="Timestamp when the script was created")
    modified_at: datetime = Field(description="Timestamp when the script was last modified")
    deleted_at: datetime | None = Field(default=None, description="Timestamp when the script was soft deleted")


class ScriptBlock(BaseModel):
    script_block_id: str
    organization_id: str
    script_id: str
    script_revision_id: str
    script_block_label: str
    script_file_id: str | None = None
    run_signature: str | None = None  # The function call code to execute this block
    workflow_run_id: str | None = None
    workflow_run_block_id: str | None = None
    input_fields: list[str] | None = None
    created_at: datetime
    modified_at: datetime
    deleted_at: datetime | None = None


class ScriptCacheKeyValuesResponse(BaseModel):
    filtered_count: int
    page: int
    page_size: int
    total_count: int
    values: list[str]


class ScriptBlocksResponse(BaseModel):
    blocks: dict[str, str]


class ScriptBlocksRequest(BaseModel):
    cache_key_value: str
    cache_key: str | None = None
    status: ScriptStatus | None = None
    workflow_run_id: str | None = None


class ScriptStatus(StrEnum):
    published = "published"
    pending = "pending"


class WorkflowScript(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    workflow_script_id: str
    organization_id: str
    script_id: str
    workflow_permanent_id: str
    workflow_id: str | None = None
    workflow_run_id: str | None = None
    cache_key: str
    cache_key_value: str
    status: ScriptStatus
    created_at: datetime
    modified_at: datetime
    deleted_at: datetime | None = None
