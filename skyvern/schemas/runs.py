from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, Union
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator

from skyvern.forge.sdk.schemas.files import FileInfo
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
    RESIDENTIAL_AU = "RESIDENTIAL_AU"
    RESIDENTIAL_ISP = "RESIDENTIAL_ISP"
    NONE = "NONE"

    @staticmethod
    def get_zone(proxy_location: ProxyLocation) -> str:
        zone_mapping = {
            ProxyLocation.US_CA: "california",
            ProxyLocation.US_NY: "newyork",
            ProxyLocation.US_TX: "texas",
            ProxyLocation.US_FL: "florida",
            ProxyLocation.US_WA: "washington",
            ProxyLocation.RESIDENTIAL: "residential_long-country-us",
        }
        if proxy_location in zone_mapping:
            return zone_mapping[proxy_location]
        raise ValueError(f"No zone mapping for proxy location: {proxy_location}")

    @classmethod
    def residential_country_locations(cls) -> set[ProxyLocation]:
        return {
            cls.RESIDENTIAL,
            cls.RESIDENTIAL_ES,
            cls.RESIDENTIAL_IE,
            cls.RESIDENTIAL_GB,
            cls.RESIDENTIAL_IN,
            cls.RESIDENTIAL_JP,
            cls.RESIDENTIAL_FR,
            cls.RESIDENTIAL_DE,
            cls.RESIDENTIAL_NZ,
            cls.RESIDENTIAL_ZA,
            cls.RESIDENTIAL_AR,
            cls.RESIDENTIAL_AU,
        }

    @staticmethod
    def get_proxy_count(proxy_location: ProxyLocation) -> int:
        counts = {
            ProxyLocation.RESIDENTIAL: 10000,
            ProxyLocation.RESIDENTIAL_ES: 2000,
            ProxyLocation.RESIDENTIAL_IE: 2000,
            ProxyLocation.RESIDENTIAL_GB: 2000,
            ProxyLocation.RESIDENTIAL_IN: 2000,
            ProxyLocation.RESIDENTIAL_JP: 2000,
            ProxyLocation.RESIDENTIAL_FR: 2000,
            ProxyLocation.RESIDENTIAL_DE: 2000,
            ProxyLocation.RESIDENTIAL_NZ: 2000,
            ProxyLocation.RESIDENTIAL_ZA: 2000,
            ProxyLocation.RESIDENTIAL_AR: 2000,
            ProxyLocation.RESIDENTIAL_AU: 2000,
        }
        return counts.get(proxy_location, 10000)

    @staticmethod
    def get_country_code(proxy_location: ProxyLocation) -> str:
        mapping = {
            ProxyLocation.RESIDENTIAL: "US",
            ProxyLocation.RESIDENTIAL_ES: "ES",
            ProxyLocation.RESIDENTIAL_IE: "IE",
            ProxyLocation.RESIDENTIAL_GB: "GB",
            ProxyLocation.RESIDENTIAL_IN: "IN",
            ProxyLocation.RESIDENTIAL_JP: "JP",
            ProxyLocation.RESIDENTIAL_FR: "FR",
            ProxyLocation.RESIDENTIAL_DE: "DE",
            ProxyLocation.RESIDENTIAL_NZ: "NZ",
            ProxyLocation.RESIDENTIAL_ZA: "ZA",
            ProxyLocation.RESIDENTIAL_AR: "AR",
            ProxyLocation.RESIDENTIAL_AU: "AU",
        }
        return mapping.get(proxy_location, "US")


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

    if proxy_location == ProxyLocation.RESIDENTIAL_AU:
        return ZoneInfo("Australia/Sydney")

    if proxy_location == ProxyLocation.RESIDENTIAL_ISP:
        return ZoneInfo("America/New_York")

    return None


class RunType(StrEnum):
    task_v1 = "task_v1"
    task_v2 = "task_v2"
    workflow_run = "workflow_run"
    openai_cua = "openai_cua"
    anthropic_cua = "anthropic_cua"
    ui_tars = "ui_tars"


class RunEngine(StrEnum):
    skyvern_v1 = "skyvern-1.0"
    skyvern_v2 = "skyvern-2.0"
    openai_cua = "openai-cua"
    anthropic_cua = "anthropic-cua"
    ui_tars = "ui-tars"


CUA_ENGINES = [RunEngine.openai_cua, RunEngine.anthropic_cua, RunEngine.ui_tars]
CUA_RUN_TYPES = [RunType.openai_cua, RunType.anthropic_cua, RunType.ui_tars]


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
        default=RunEngine.skyvern_v2,
        description=TASK_ENGINE_DOC_STRING,
    )
    title: str | None = Field(
        default=None, description="The title for the task", examples=["The title of my first skyvern task"]
    )
    proxy_location: ProxyLocation | None = Field(
        default=ProxyLocation.RESIDENTIAL,
        description=PROXY_LOCATION_DOC_STRING,
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
    workflow_id: str = Field(
        description="ID of the workflow to run. Workflow ID starts with `wpid_`.", examples=["wpid_123"]
    )
    parameters: dict[str, Any] | None = Field(default=None, description="Parameters to pass to the workflow")
    title: str | None = Field(default=None, description="The title for this workflow run")
    proxy_location: ProxyLocation | None = Field(
        default=ProxyLocation.RESIDENTIAL,
        description=PROXY_LOCATION_DOC_STRING,
    )
    webhook_url: str | None = Field(
        default=None,
        description="URL to send workflow status updates to after a run is finished. Refer to https://docs.skyvern.com/running-tasks/webhooks-faq for webhook questions.",
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
    max_screenshot_scrolls: int | None = Field(
        default=None,
        description="The maximum number of scrolls for the post action screenshot. When it's None or 0, it takes the current viewpoint screenshot.",
    )
    extra_http_headers: dict[str, str] | None = Field(
        default=None,
        description="The extra HTTP headers for the requests in browser.",
    )

    @field_validator("webhook_url", "totp_url")
    @classmethod
    def validate_urls(cls, url: str | None) -> str | None:
        if url is None:
            return None
        return validate_url(url)


class BlockRunRequest(WorkflowRunRequest):
    block_labels: list[str] = Field(
        description="Labels of the blocks to execute",
        examples=["block_1", "block_2"],
    )


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
    max_screenshot_scrolls: int | None = Field(
        default=None,
        description="The maximum number of scrolls for the post action screenshot. When it's None or 0, it takes the current viewpoint screenshot",
    )


class TaskRunResponse(BaseRunResponse):
    run_type: Literal[RunType.task_v1, RunType.task_v2, RunType.openai_cua, RunType.anthropic_cua, RunType.ui_tars] = (
        Field(description="Types of a task run - task_v1, task_v2, openai_cua, anthropic_cua, ui_tars")
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


class BlockRunResponse(WorkflowRunResponse):
    block_labels: list[str] = Field(description="A whitelist of block labels; only these blocks will execute")
