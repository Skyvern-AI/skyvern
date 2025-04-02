from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, Union
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator

from skyvern.forge.sdk.schemas.files import FileInfo
from skyvern.utils.url_validators import validate_url


class ProxyLocation(StrEnum):
    RESIDENTIAL = "RESIDENTIAL"
    US_CA = "US-CA"
    US_NY = "US-NY"
    US_TX = "US-TX"
    US_FL = "US-FL"
    US_WA = "US-WA"
    RESIDENTIAL_ES = "RESIDENTIAL_ES"
    RESIDENTIAL_IE = "RESIDENTIAL_IE"
    RESIDENTIAL_GB = "RESIDENTIAL_GB"
    RESIDENTIAL_IN = "RESIDENTIAL_IN"
    RESIDENTIAL_JP = "RESIDENTIAL_JP"
    RESIDENTIAL_FR = "RESIDENTIAL_FR"
    RESIDENTIAL_DE = "RESIDENTIAL_DE"
    RESIDENTIAL_NZ = "RESIDENTIAL_NZ"
    RESIDENTIAL_ZA = "RESIDENTIAL_ZA"
    RESIDENTIAL_AR = "RESIDENTIAL_AR"
    RESIDENTIAL_ISP = "RESIDENTIAL_ISP"
    NONE = "NONE"


def get_tzinfo_from_proxy(proxy_location: ProxyLocation) -> ZoneInfo | None:
    if proxy_location == ProxyLocation.NONE:
        return None

    if proxy_location == ProxyLocation.US_CA:
        return ZoneInfo("America/Los_Angeles")

    if proxy_location == ProxyLocation.US_NY:
        return ZoneInfo("America/New_York")

    if proxy_location == ProxyLocation.US_TX:
        return ZoneInfo("America/Chicago")

    if proxy_location == ProxyLocation.US_FL:
        return ZoneInfo("America/New_York")

    if proxy_location == ProxyLocation.US_WA:
        return ZoneInfo("America/New_York")

    if proxy_location == ProxyLocation.RESIDENTIAL:
        return ZoneInfo("America/New_York")

    if proxy_location == ProxyLocation.RESIDENTIAL_ES:
        return ZoneInfo("Europe/Madrid")

    if proxy_location == ProxyLocation.RESIDENTIAL_IE:
        return ZoneInfo("Europe/Dublin")

    if proxy_location == ProxyLocation.RESIDENTIAL_GB:
        return ZoneInfo("Europe/London")

    if proxy_location == ProxyLocation.RESIDENTIAL_IN:
        return ZoneInfo("Asia/Kolkata")

    if proxy_location == ProxyLocation.RESIDENTIAL_JP:
        return ZoneInfo("Asia/Tokyo")

    if proxy_location == ProxyLocation.RESIDENTIAL_FR:
        return ZoneInfo("Europe/Paris")

    if proxy_location == ProxyLocation.RESIDENTIAL_DE:
        return ZoneInfo("Europe/Berlin")

    if proxy_location == ProxyLocation.RESIDENTIAL_NZ:
        return ZoneInfo("Pacific/Auckland")

    if proxy_location == ProxyLocation.RESIDENTIAL_ZA:
        return ZoneInfo("Africa/Johannesburg")

    if proxy_location == ProxyLocation.RESIDENTIAL_AR:
        return ZoneInfo("America/Argentina/Buenos_Aires")

    if proxy_location == ProxyLocation.RESIDENTIAL_ISP:
        return ZoneInfo("America/New_York")

    return None


class RunType(StrEnum):
    task_v1 = "task_v1"
    task_v2 = "task_v2"
    workflow_run = "workflow_run"


class RunEngine(StrEnum):
    skyvern_v1 = "skyvern-1.0"
    skyvern_v2 = "skyvern-2.0"


class RunStatus(StrEnum):
    created = "created"
    queued = "queued"
    running = "running"
    timed_out = "timed_out"
    failed = "failed"
    terminated = "terminated"
    completed = "completed"
    canceled = "canceled"

    def is_final(self) -> bool:
        return self in [self.failed, self.terminated, self.canceled, self.timed_out, self.completed]


class TaskRunRequest(BaseModel):
    prompt: str = Field(description="The goal or task description for Skyvern to accomplish")
    url: str | None = Field(
        default=None,
        description="The starting URL for the task. If not provided, Skyvern will attempt to determine an appropriate URL",
    )
    title: str | None = Field(default=None, description="Optional title for the task")
    engine: RunEngine = Field(
        default=RunEngine.skyvern_v2, description="The Skyvern engine version to use for this task"
    )
    proxy_location: ProxyLocation | None = Field(
        default=ProxyLocation.RESIDENTIAL, description="Geographic Proxy location to route the browser traffic through"
    )
    data_extraction_schema: dict | list | str | None = Field(
        default=None, description="Schema defining what data should be extracted from the webpage"
    )
    error_code_mapping: dict[str, str] | None = Field(
        default=None, description="Custom mapping of error codes to error messages if Skyvern encounters an error"
    )
    max_steps: int | None = Field(
        default=None, description="Maximum number of steps the task can take before timing out"
    )
    webhook_url: str | None = Field(
        default=None, description="URL to send task status updates to after a run is finished"
    )
    totp_identifier: str | None = Field(
        default=None,
        description="Identifier for TOTP (Time-based One-Time Password) authentication if codes are being pushed to Skyvern",
    )
    totp_url: str | None = Field(
        default=None,
        description="URL for TOTP authentication setup if Skyvern should be polling endpoint for 2FA codes",
    )
    browser_session_id: str | None = Field(
        default=None,
        description="ID of an existing browser session to reuse, having it continue from the current screen state",
    )
    publish_workflow: bool = Field(default=False, description="Whether to publish this task as a reusable workflow. ")

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
        if url is None:
            return None

        return validate_url(url)


class WorkflowRunRequest(BaseModel):
    workflow_id: str = Field(description="ID of the workflow to run")
    title: str | None = Field(default=None, description="Optional title for this workflow run")
    parameters: dict[str, Any] = Field(default={}, description="Parameters to pass to the workflow")
    proxy_location: ProxyLocation = Field(
        default=ProxyLocation.RESIDENTIAL, description="Location of proxy to use for this workflow run"
    )
    webhook_url: str | None = Field(
        default=None, description="URL to send workflow status updates to after a run is finished"
    )
    totp_url: str | None = Field(
        default=None,
        description="URL for TOTP authentication setup if Skyvern should be polling endpoint for 2FA codes",
    )
    totp_identifier: str | None = Field(
        default=None,
        description="Identifier for TOTP (Time-based One-Time Password) authentication if codes are being pushed to Skyvern",
    )
    browser_session_id: str | None = Field(
        default=None,
        description="ID of an existing browser session to reuse, having it continue from the current screen state",
    )

    @field_validator("webhook_url", "totp_url")
    @classmethod
    def validate_urls(cls, url: str | None) -> str | None:
        if url is None:
            return None
        return validate_url(url)


class BaseRunResponse(BaseModel):
    run_id: str = Field(description="Unique identifier for this run")
    status: RunStatus = Field(description="Current status of the run")
    output: dict | list | str | None = Field(
        default=None, description="Output data from the run, if any. Format depends on the schema in the input"
    )
    downloaded_files: list[FileInfo] | None = Field(default=None, description="List of files downloaded during the run")
    recording_url: str | None = Field(default=None, description="URL to the recording of the run")
    failure_reason: str | None = Field(default=None, description="Reason for failure if the run failed")
    created_at: datetime = Field(description="Timestamp when this run was created")
    modified_at: datetime = Field(description="Timestamp when this run was last modified")


class TaskRunResponse(BaseRunResponse):
    run_type: Literal[RunType.task_v1, RunType.task_v2] = Field(
        description="Type of task run - either task_v1 or task_v2"
    )
    run_request: TaskRunRequest | None = Field(
        default=None, description="The original request parameters used to start this task run"
    )


class WorkflowRunResponse(BaseRunResponse):
    run_type: Literal[RunType.workflow_run] = Field(description="Type of run - always workflow_run for workflow runs")
    run_request: WorkflowRunRequest | None = Field(
        default=None, description="The original request parameters used to start this workflow run"
    )


RunResponse = Annotated[Union[TaskRunResponse, WorkflowRunResponse], Field(discriminator="run_type")]
