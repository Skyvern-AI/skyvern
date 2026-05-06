from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Any, Literal, TypeAlias, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from skyvern.forge.sdk.schemas.files import FileInfo
from skyvern.forge.sdk.workflow.models.validators import normalize_run_with
from skyvern.schemas.docs.doc_examples import (
    BROWSER_SESSION_ID_EXAMPLES,
    ERROR_CODE_MAPPING_EXAMPLES,
    TASK_PROMPT_EXAMPLES,
    TASK_URL_EXAMPLES,
    TOTP_IDENTIFIER_EXAMPLES,
    TOTP_URL_EXAMPLES,
)
from skyvern.schemas.docs.doc_strings import (
    BROWSER_SESSION_ID_DOC_STRING,
    DATA_EXTRACTION_SCHEMA_DOC_STRING,
    ERROR_CODE_MAPPING_DOC_STRING,
    MAX_STEPS_DOC_STRING,
    MODEL_CONFIG,
    PROXY_LOCATION_DOC_STRING,
    TASK_ENGINE_DOC_STRING,
    TASK_PROMPT_DOC_STRING,
    TASK_URL_DOC_STRING,
    TOTP_IDENTIFIER_DOC_STRING,
    TOTP_URL_DOC_STRING,
    WEBHOOK_URL_DOC_STRING,
)
from skyvern.schemas.proxy_location import (  # noqa: F401
    SUPPORTED_GEO_COUNTRIES,
    GeoTarget,
    ProxyLocation,
    ProxyLocationInput,
    get_tzinfo_from_proxy,
    proxy_location_to_request,
)
from skyvern.schemas.run_enums import (  # noqa: F401
    CUA_ENGINES,
    CUA_RUN_TYPES,
    TERMINAL_STATUSES,
    RunEngine,
    RunStatus,
    RunType,
)
from skyvern.utils.url_validators import validate_url

# Type checkers need string Literal values, while pydantic's discriminated
# union preserves enum instances when runtime Literals use the enum members.
if TYPE_CHECKING:
    TaskRunTypeField: TypeAlias = Literal["task_v1", "task_v2", "openai_cua", "anthropic_cua", "ui_tars"]
    WorkflowRunTypeField: TypeAlias = Literal["workflow_run"]
else:
    TaskRunTypeField = Literal[
        RunType.task_v1,
        RunType.task_v2,
        RunType.openai_cua,
        RunType.anthropic_cua,
        RunType.ui_tars,
    ]
    WorkflowRunTypeField = Literal[RunType.workflow_run]


class TaskRunRequest(BaseModel):
    prompt: str = Field(
        description=TASK_PROMPT_DOC_STRING,
        examples=TASK_PROMPT_EXAMPLES,
    )
    url: str | None = Field(
        default=None,
        description=TASK_URL_DOC_STRING,
        examples=TASK_URL_EXAMPLES,
    )
    engine: RunEngine = Field(
        default=RunEngine.skyvern_v1,
        description=TASK_ENGINE_DOC_STRING,
    )
    title: str | None = Field(
        default=None, description="The title for the task", examples=["The title of my first skyvern task"]
    )
    proxy_location: ProxyLocationInput = Field(
        default=ProxyLocation.RESIDENTIAL,
        description=PROXY_LOCATION_DOC_STRING + " Can also be a GeoTarget object for granular city/state targeting: "
        '{"country": "US", "subdivision": "CA", "city": "San Francisco"}',
    )
    data_extraction_schema: dict | list | str | None = Field(
        default=None,
        description=DATA_EXTRACTION_SCHEMA_DOC_STRING,
    )
    error_code_mapping: dict[str, str] | None = Field(
        default=None,
        description=ERROR_CODE_MAPPING_DOC_STRING,
        examples=ERROR_CODE_MAPPING_EXAMPLES,
    )
    max_steps: int | None = Field(
        default=None,
        description=MAX_STEPS_DOC_STRING,
        examples=[10, 25],
    )
    webhook_url: str | None = Field(
        default=None,
        description=WEBHOOK_URL_DOC_STRING,
        examples=["https://my-site.com/webhook"],
    )
    totp_identifier: str | None = Field(
        default=None,
        description=TOTP_IDENTIFIER_DOC_STRING,
        examples=TOTP_IDENTIFIER_EXAMPLES,
    )
    totp_url: str | None = Field(
        default=None,
        description=TOTP_URL_DOC_STRING,
        examples=TOTP_URL_EXAMPLES,
    )
    browser_session_id: str | None = Field(
        default=None,
        description=BROWSER_SESSION_ID_DOC_STRING,
        examples=BROWSER_SESSION_ID_EXAMPLES,
    )
    model: dict[str, Any] | None = Field(
        default=None,
        description=MODEL_CONFIG,
        examples=None,
    )
    extra_http_headers: dict[str, str] | None = Field(
        default=None,
        description="The extra HTTP headers for the requests in browser.",
    )
    publish_workflow: bool = Field(
        default=False,
        description="Whether to publish this task as a reusable workflow. Only available for skyvern-2.0.",
    )
    include_action_history_in_verification: bool | None = Field(
        default=False, description="Whether to include action history when verifying that the task is complete"
    )
    max_screenshot_scrolls: int | None = Field(
        default=None,
        description="The maximum number of scrolls for the post action screenshot. When it's None or 0, it takes the current viewpoint screenshot.",
    )
    browser_address: str | None = Field(
        default=None,
        description="The CDP address for the task.",
        examples=["http://127.0.0.1:9222", "ws://127.0.0.1:9222/devtools/browser/1234567890"],
    )
    run_with: str | None = Field(
        default=None,
        description="Whether to run the task with agent or code. Null means use the default.",
        examples=["agent", "code"],
    )

    @field_validator("run_with", mode="before")
    @classmethod
    def _normalize_run_with(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return normalize_run_with(v)

    @field_validator("url", "webhook_url", "totp_url")
    @classmethod
    def validate_urls(cls, url: str | None) -> str | None:
        """
        Validates that URLs provided to Skyvern are properly formatted.

        Args:
            url: The URL for Skyvern to validate

        Returns:
            The validated URL or None if no URL was provided
        """
        if not url:
            return url

        return validate_url(url)

    @model_validator(mode="after")
    def _force_v2_for_publish_workflow(self) -> TaskRunRequest:
        if self.publish_workflow and self.engine != RunEngine.skyvern_v2:
            self.engine = RunEngine.skyvern_v2
        return self


class WorkflowRunRequest(BaseModel):
    workflow_id: str = Field(
        description="ID of the workflow to run. Workflow ID starts with `wpid_`.", examples=["wpid_123"]
    )
    parameters: dict[str, Any] | None = Field(default=None, description="Parameters to pass to the workflow")
    title: str | None = Field(default=None, description="The title for this workflow run")
    proxy_location: ProxyLocationInput = Field(
        default=ProxyLocation.RESIDENTIAL,
        description=PROXY_LOCATION_DOC_STRING + " Can also be a GeoTarget object for granular city/state targeting: "
        '{"country": "US", "subdivision": "CA", "city": "San Francisco"}',
    )
    webhook_url: str | None = Field(
        default=None,
        description="URL to send workflow status updates to after a run is finished. Refer to https://www.skyvern.com/docs/running-tasks/webhooks-faq for webhook questions.",
    )
    totp_url: str | None = Field(
        default=None,
        description=TOTP_URL_DOC_STRING,
        examples=TOTP_URL_EXAMPLES,
    )
    totp_identifier: str | None = Field(
        default=None,
        description=TOTP_IDENTIFIER_DOC_STRING,
        examples=TOTP_IDENTIFIER_EXAMPLES,
    )
    browser_session_id: str | None = Field(
        default=None,
        description="ID of a Skyvern browser session to reuse, having it continue from the current screen state",
    )
    browser_profile_id: str | None = Field(
        default=None,
        description="ID of a browser profile to reuse for this workflow run",
    )
    max_screenshot_scrolls: int | None = Field(
        default=None,
        description="The maximum number of scrolls for the post action screenshot. When it's None or 0, it takes the current viewpoint screenshot.",
    )
    extra_http_headers: dict[str, str] | None = Field(
        default=None,
        description="The extra HTTP headers for the requests in browser.",
    )
    browser_address: str | None = Field(
        default=None,
        description="The CDP address for the workflow run.",
        examples=["http://127.0.0.1:9222", "ws://127.0.0.1:9222/devtools/browser/1234567890"],
    )
    ai_fallback: bool | None = Field(
        default=None,
        description="Whether to fallback to AI if the workflow run fails.",
    )
    run_with: str | None = Field(
        default=None,
        description="Whether to run the workflow with agent or code. Null inherits from the workflow setting.",
        examples=["agent", "code"],
    )

    @field_validator("run_with", mode="before")
    @classmethod
    def _normalize_run_with(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return normalize_run_with(v)

    @field_validator("webhook_url", "totp_url")
    @classmethod
    def validate_urls(cls, url: str | None) -> str | None:
        if not url:
            return url
        return validate_url(url)


class BlockRunRequest(WorkflowRunRequest):
    block_labels: list[str] = Field(
        description="Labels of the blocks to execute",
        examples=["block_1", "block_2"],
    )
    block_outputs: dict[str, Any] | None = Field(
        default=None,
        # NOTE(jdo): this is either the last output of the block for a given
        # org_id/user_id, or an override supplied by the user
        description="Any active outputs of blocks in a workflow being debugged",
    )
    code_gen: bool | None = Field(
        default=False,
        description="Whether to generate colde for blocks that support it",
    )
    debug_session_id: str | None = Field(
        default=None,
        description="ID of the debug session to use for this block run",
    )


class ScriptRunResponse(BaseModel):
    # `extra="ignore"` is the Pydantic v2 default; making it explicit
    # pins the forward-compat guarantee (unknown keys silently dropped).
    model_config = ConfigDict(extra="ignore")

    # True iff a fallback fired during this run, flipping at least one
    # block's execution from cached script to the agent. Writers: the two
    # `services/script_service.py` fallback paths (script-block failure +
    # conditional-agent episode) and the `_execute_single_block` script-
    # failure path. `False` here does NOT imply "no AI execution" — blocks
    # that were ALWAYS-agent (via `requires_agent`, `disable_cache`, or
    # non-cacheable block types) never create a fallback episode and don't
    # flip this flag. For per-block routing ground truth, consult the
    # `Block execution mode resolved` log emitted at per-block execution
    # time in `skyvern/forge/sdk/workflow/service.py`.
    ai_fallback_triggered: bool = False

    # Identity of the cached script that was loaded for this run at
    # workflow setup time. Non-null iff a script was loaded. Does NOT
    # imply that every (or any) block actually executed from that cache —
    # per-block `block_labels` filtering, `requires_agent`, `disable_cache`,
    # or non-cacheable block types can still route individual blocks to AI.
    # Populated by the server-side execution path (workflow/service.py) and
    # the local CLI entrypoint (services/script_service.run_script). None
    # on rows written by older code paths that only recorded
    # `ai_fallback_triggered`.
    script_id: str | None = None
    script_revision_id: str | None = None


class UploadFileResponse(BaseModel):
    s3_uri: str = Field(description="S3 URI where the file was uploaded")
    presigned_url: str = Field(description="Presigned URL to access the uploaded file")


class BaseRunResponse(BaseModel):
    run_id: str = Field(
        description="Unique identifier for this run. Run ID starts with `tsk_` for task runs and `wr_` for workflow runs.",
        examples=["tsk_123", "tsk_v2_123", "wr_123"],
    )
    status: RunStatus = Field(
        description="Current status of the run",
        examples=["created", "queued", "running", "timed_out", "failed", "terminated", "completed", "canceled"],
    )
    output: dict | list | str | None = Field(
        default=None,
        description="Output data from the run, if any. Format/schema depends on the data extracted by the run.",
    )
    downloaded_files: list[FileInfo] | None = Field(default=None, description="List of files downloaded during the run")
    recording_url: str | None = Field(default=None, description="URL to the recording of the run")
    screenshot_urls: list[str] | None = Field(
        default=None,
        description="List of last n screenshot URLs in reverse chronological order - the first one the list is the latest screenshot.",
    )
    failure_reason: str | None = Field(default=None, description="Reason for failure if the run failed or terminated")
    created_at: datetime = Field(description="Timestamp when this run was created", examples=["2025-01-01T00:00:00Z"])
    modified_at: datetime = Field(
        description="Timestamp when this run was last modified", examples=["2025-01-01T00:05:00Z"]
    )
    queued_at: datetime | None = Field(default=None, description="Timestamp when this run was queued")
    started_at: datetime | None = Field(default=None, description="Timestamp when this run started execution")
    finished_at: datetime | None = Field(default=None, description="Timestamp when this run finished")
    app_url: str | None = Field(
        default=None,
        description="URL to the application UI where the run can be viewed",
        examples=["https://app.skyvern.com/tasks/tsk_123", "https://app.skyvern.com/workflows/wpid_123/wr_123"],
    )
    browser_session_id: str | None = Field(
        default=None, description="ID of the Skyvern persistent browser session used for this run", examples=["pbs_123"]
    )
    browser_profile_id: str | None = Field(
        default=None,
        description="ID of the browser profile used for this run",
        examples=["bp_123"],
    )
    max_screenshot_scrolls: int | None = Field(
        default=None,
        description="The maximum number of scrolls for the post action screenshot. When it's None or 0, it takes the current viewpoint screenshot",
    )
    script_run: ScriptRunResponse | None = Field(
        default=None,
        description="The script run result",
    )
    errors: list[dict[str, Any]] | None = Field(
        default=None,
        description="The errors for the run",
    )
    step_count: int | None = Field(
        default=None,
        description="Total number of steps executed in this run",
    )


class TaskRunResponse(BaseRunResponse):
    run_type: TaskRunTypeField = Field(
        description="Types of a task run - task_v1, task_v2, openai_cua, anthropic_cua, ui_tars"
    )
    run_request: TaskRunRequest | None = Field(
        default=None, description="The original request parameters used to start this task run"
    )


class WorkflowRunResponse(BaseRunResponse):
    run_type: WorkflowRunTypeField = Field(description="Type of run - always workflow_run for workflow runs")
    run_with: str = Field(
        default="agent",
        description="Whether the workflow run was executed with agent or code",
        examples=["agent", "code"],
    )

    @field_validator("run_with", mode="before")
    @classmethod
    def _normalize_run_with(cls, v: str | None) -> str:
        return normalize_run_with(v)

    ai_fallback: bool | None = Field(
        default=None,
        description="Whether to fallback to AI if code run fails.",
    )
    run_request: WorkflowRunRequest | None = Field(
        default=None, description="The original request parameters used to start this workflow run"
    )


RunResponse = Annotated[Union[TaskRunResponse, WorkflowRunResponse], Field(discriminator="run_type")]


class BlockRunResponse(WorkflowRunResponse):
    block_labels: list[str] = Field(description="A whitelist of block labels; only these blocks will execute")


class TaskRunListItem(BaseModel):
    """Lightweight run-history item backed by the task_runs table."""

    model_config = ConfigDict(from_attributes=True)

    task_run_id: str
    run_id: str
    task_run_type: str
    status: str
    title: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime
    workflow_permanent_id: str | None = None
    workflow_deleted: bool = False
    script_run: bool = False
    searchable_text: str | None = Field(default=None, exclude=True)

    @field_validator("script_run", mode="before")
    @classmethod
    def coerce_script_run(cls, v: Any) -> bool:
        """Intentionally lossy: collapse dict metadata / bool / None → bool for the list view.

        The full script execution metadata (dict) is available via the detail
        endpoint's Run.script_run field.  Do not rely on dict contents here.
        """
        return bool(v)
