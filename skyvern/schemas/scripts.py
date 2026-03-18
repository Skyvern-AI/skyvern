from __future__ import annotations

import base64
from datetime import datetime
from enum import StrEnum
from typing import Literal

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


class ScriptVersionSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    version: int
    script_revision_id: str
    created_at: datetime
    run_id: str | None = None


class ScriptVersionListResponse(BaseModel):
    versions: list[ScriptVersionSummary]


class ScriptVersionDetailResponse(BaseModel):
    """Full detail for a single script version, including code and metadata."""

    script_id: str
    script_revision_id: str
    version: int
    created_at: datetime
    run_id: str | None = None
    blocks: dict[str, str]
    main_script: str | None = None
    fallback_episode_count: int = 0


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
    requires_agent: bool = False  # When True, block must run via agent even in code mode
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
    main_script: str | None = None
    script_id: str | None = None
    version: int | None = None


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
    is_pinned: bool = False
    pinned_at: datetime | None = None
    pinned_by: str | None = None
    created_at: datetime
    modified_at: datetime
    deleted_at: datetime | None = None


class WorkflowScriptSummary(BaseModel):
    """Summary of a workflow script (cache key variant) with version info."""

    script_id: str
    cache_key: str
    cache_key_value: str
    status: ScriptStatus
    latest_version: int
    version_count: int
    total_runs: int = 0
    success_rate: float | None = None
    is_pinned: bool = False
    created_at: datetime
    modified_at: datetime


class WorkflowScriptsListResponse(BaseModel):
    """Response for listing all scripts associated with a workflow."""

    scripts: list[WorkflowScriptSummary]


class ScriptRunSummary(BaseModel):
    """Summary of a workflow run that used a specific script."""

    workflow_run_id: str
    status: str
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime
    failure_reason: str | None = None


class ScriptRunsResponse(BaseModel):
    """Response for listing runs associated with a script."""

    runs: list[ScriptRunSummary]
    total_count: int
    status_counts: dict[str, int] = Field(default_factory=dict)


class ClearCacheResponse(BaseModel):
    """Response model for cache clearing operations."""

    deleted_count: int = Field(..., description="Number of cached entries deleted")
    message: str = Field(..., description="Status message")


class ScriptFallbackEpisode(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    episode_id: str
    organization_id: str
    workflow_permanent_id: str
    workflow_run_id: str
    script_revision_id: str | None = None
    block_label: str
    fallback_type: Literal["element", "full_block", "conditional_agent"]
    error_message: str | None = None
    classify_result: str | None = None
    agent_actions: list | dict | None = None
    page_url: str | None = None
    page_text_snapshot: str | None = None
    fallback_succeeded: bool | None = None
    reviewed: bool = False
    reviewer_output: str | None = None
    new_script_revision_id: str | None = None
    created_at: datetime
    modified_at: datetime


class FallbackEpisodeListResponse(BaseModel):
    episodes: list[ScriptFallbackEpisode]
    page: int
    page_size: int
    total_count: int


class ScriptBranchHit(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    organization_id: str
    workflow_permanent_id: str
    block_label: str
    branch_key: str
    hit_count: int = 1
    first_hit_at: datetime
    last_hit_at: datetime


class ScriptVersionCompareResponse(BaseModel):
    """Response containing two script versions for comparison."""

    script_id: str
    base_version: int
    base_blocks: dict[str, str]
    base_main_script: str | None = None
    base_created_at: datetime
    base_run_id: str | None = None
    compare_version: int
    compare_blocks: dict[str, str]
    compare_main_script: str | None = None
    compare_created_at: datetime
    compare_run_id: str | None = None


class ReviewScriptRequest(BaseModel):
    """Request body for user-initiated script review."""

    user_instructions: str = Field(
        ...,
        min_length=1,
        max_length=5000,
        description="Instructions for how to fix the script",
    )
    workflow_run_id: str | None = Field(
        None,
        description="Workflow run ID to pull fallback episodes from (optional)",
    )


class ReviewScriptResponse(BaseModel):
    """Response from a user-initiated script review."""

    script_id: str
    version: int
    updated_blocks: list[str]
    message: str | None = None


class PinScriptRequest(BaseModel):
    """Request to pin a specific cache key variant's script."""

    cache_key_value: str = Field(..., description="The cache key value to pin")


class PinScriptResponse(BaseModel):
    """Response after pinning/unpinning a script."""

    workflow_permanent_id: str
    cache_key_value: str
    is_pinned: bool
    pinned_at: datetime | None = None
