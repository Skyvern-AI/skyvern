from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator

from skyvern.exceptions import InvalidTaskStatusTransition, TaskAlreadyCanceled, TaskAlreadyTimeout
from skyvern.forge.sdk.core.validators import validate_url
from skyvern.forge.sdk.db.enums import TaskType
from skyvern.forge.sdk.schemas.files import FileInfo


class ProxyLocation(StrEnum):
    US_CA = "US-CA"
    US_NY = "US-NY"
    US_TX = "US-TX"
    US_FL = "US-FL"
    US_WA = "US-WA"
    RESIDENTIAL = "RESIDENTIAL"
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


class TaskBase(BaseModel):
    title: str | None = Field(
        default=None,
        description="The title of the task.",
        examples=["Get a quote for car insurance"],
    )
    url: str = Field(
        ...,
        description="Starting URL for the task.",
        examples=["https://www.geico.com"],
    )
    webhook_callback_url: str | None = Field(
        default=None,
        description="The URL to call when the task is completed.",
        examples=["https://my-webhook.com"],
    )
    totp_verification_url: str | None = None
    totp_identifier: str | None = None
    navigation_goal: str | None = Field(
        default=None,
        description="The user's goal for the task.",
        examples=["Get a quote for car insurance"],
    )
    data_extraction_goal: str | None = Field(
        default=None,
        description="The user's goal for data extraction.",
        examples=["Extract the quote price"],
    )
    navigation_payload: dict[str, Any] | list | str | None = Field(
        default=None,
        description="The user's details needed to achieve the task.",
        examples=[{"name": "John Doe", "email": "john@doe.com"}],
    )
    error_code_mapping: dict[str, str] | None = Field(
        default=None,
        description="The mapping of error codes and their descriptions.",
        examples=[
            {
                "out_of_stock": "Return this error when the product is out of stock",
                "not_found": "Return this error when the product is not found",
            }
        ],
    )
    proxy_location: ProxyLocation | None = Field(
        default=None,
        description="The location of the proxy to use for the task.",
        examples=["US-WA", "US-CA", "US-FL", "US-NY", "US-TX"],
    )
    extracted_information_schema: dict[str, Any] | list | str | None = Field(
        default=None,
        description="The requested schema of the extracted information.",
    )
    complete_criterion: str | None = Field(
        default=None, description="Criterion to complete", examples=["Complete if 'hello world' shows up on the page"]
    )
    terminate_criterion: str | None = Field(
        default=None,
        description="Criterion to terminate",
        examples=["Terminate if 'existing account' shows up on the page"],
    )
    task_type: TaskType | None = Field(
        default=TaskType.general,
        description="The type of the task",
        examples=[TaskType.general, TaskType.validation],
    )
    application: str | None = Field(
        default=None,
        description="The application for which the task is running",
        examples=["forms"],
    )


class TaskRequest(TaskBase):
    url: str = Field(
        ...,
        description="Starting URL for the task.",
        examples=["https://www.geico.com"],
    )
    webhook_callback_url: str | None = Field(
        default=None,
        description="The URL to call when the task is completed.",
        examples=["https://my-webhook.com"],
    )
    totp_verification_url: str | None = None
    browser_session_id: str | None = None

    @field_validator("url", "webhook_callback_url", "totp_verification_url")
    @classmethod
    def validate_urls(cls, url: str | None) -> str | None:
        if url is None:
            return None

        return validate_url(url)


class TaskStatus(StrEnum):
    created = "created"
    queued = "queued"
    running = "running"
    timed_out = "timed_out"
    failed = "failed"
    terminated = "terminated"
    completed = "completed"
    canceled = "canceled"

    def is_final(self) -> bool:
        return self in {
            TaskStatus.failed,
            TaskStatus.terminated,
            TaskStatus.completed,
            TaskStatus.timed_out,
            TaskStatus.canceled,
        }

    def can_update_to(self, new_status: TaskStatus) -> bool:
        allowed_transitions: dict[TaskStatus, set[TaskStatus]] = {
            TaskStatus.created: {
                TaskStatus.queued,
                TaskStatus.running,
                TaskStatus.timed_out,
                TaskStatus.failed,
                TaskStatus.canceled,
            },
            TaskStatus.queued: {
                TaskStatus.running,
                TaskStatus.timed_out,
                TaskStatus.failed,
                TaskStatus.canceled,
            },
            TaskStatus.running: {
                TaskStatus.completed,
                TaskStatus.failed,
                TaskStatus.terminated,
                TaskStatus.timed_out,
                TaskStatus.canceled,
            },
            TaskStatus.failed: set(),
            TaskStatus.terminated: set(),
            TaskStatus.completed: set(),
            TaskStatus.timed_out: set(),
            TaskStatus.canceled: {TaskStatus.completed},
        }
        return new_status in allowed_transitions[self]

    def requires_extracted_info(self) -> bool:
        status_requires_extracted_information = {TaskStatus.completed}
        return self in status_requires_extracted_information

    def cant_have_extracted_info(self) -> bool:
        status_cant_have_extracted_information = {
            TaskStatus.created,
            TaskStatus.queued,
            TaskStatus.running,
            TaskStatus.failed,
            TaskStatus.terminated,
        }
        return self in status_cant_have_extracted_information

    def requires_failure_reason(self) -> bool:
        status_requires_failure_reason = {TaskStatus.failed, TaskStatus.terminated}
        return self in status_requires_failure_reason


class Task(TaskBase):
    created_at: datetime = Field(
        ...,
        description="The creation datetime of the task.",
        examples=["2023-01-01T00:00:00Z"],
    )
    modified_at: datetime = Field(
        ...,
        description="The modification datetime of the task.",
        examples=["2023-01-01T00:00:00Z"],
    )
    task_id: str = Field(
        ...,
        description="The ID of the task.",
        examples=["50da533e-3904-4401-8a07-c49adf88b5eb"],
    )
    status: TaskStatus = Field(..., description="The status of the task.", examples=["created"])
    extracted_information: dict[str, Any] | list | str | None = Field(
        None,
        description="The extracted information from the task.",
    )
    failure_reason: str | None = Field(
        None,
        description="The reason for the task failure.",
    )
    organization_id: str | None = None
    workflow_run_id: str | None = None
    order: int | None = None
    retry: int | None = None
    max_steps_per_run: int | None = None
    errors: list[dict[str, Any]] = []

    def validate_update(
        self,
        status: TaskStatus,
        extracted_information: dict[str, Any] | list | str | None,
        failure_reason: str | None = None,
    ) -> None:
        old_status = self.status

        if not old_status.can_update_to(status):
            if old_status == TaskStatus.canceled:
                raise TaskAlreadyCanceled(new_status=status, task_id=self.task_id)
            if old_status == TaskStatus.timed_out:
                raise TaskAlreadyTimeout(task_id=self.task_id)
            raise InvalidTaskStatusTransition(old_status=old_status, new_status=status, task_id=self.task_id)

        if status.requires_failure_reason() and failure_reason is None:
            raise ValueError(f"status_requires_failure_reason({status},{self.task_id}")

        if status.requires_extracted_info() and self.data_extraction_goal and extracted_information is None:
            raise ValueError(f"status_requires_extracted_information({status},{self.task_id}")

        if status.cant_have_extracted_info() and extracted_information is not None:
            raise ValueError(f"status_cant_have_extracted_information({self.task_id})")

        if self.extracted_information is not None and extracted_information is not None:
            raise ValueError(f"cant_override_extracted_information({self.task_id})")

        if self.failure_reason is not None and failure_reason is not None:
            raise ValueError(f"cant_override_failure_reason({self.task_id})")

    def to_task_response(
        self,
        action_screenshot_urls: list[str] | None = None,
        screenshot_url: str | None = None,
        recording_url: str | None = None,
        browser_console_log_url: str | None = None,
        downloaded_files: list[FileInfo] | None = None,
        failure_reason: str | None = None,
    ) -> TaskResponse:
        return TaskResponse(
            request=self,
            task_id=self.task_id,
            status=self.status,
            created_at=self.created_at,
            modified_at=self.modified_at,
            extracted_information=self.extracted_information,
            failure_reason=failure_reason or self.failure_reason,
            action_screenshot_urls=action_screenshot_urls,
            screenshot_url=screenshot_url,
            recording_url=recording_url,
            browser_console_log_url=browser_console_log_url,
            downloaded_files=downloaded_files,
            downloaded_file_urls=[file.url for file in downloaded_files] if downloaded_files else None,
            errors=self.errors,
            max_steps_per_run=self.max_steps_per_run,
            workflow_run_id=self.workflow_run_id,
        )


class TaskResponse(BaseModel):
    request: TaskBase
    task_id: str
    status: TaskStatus
    created_at: datetime
    modified_at: datetime
    extracted_information: list | dict[str, Any] | str | None = None
    action_screenshot_urls: list[str] | None = None
    screenshot_url: str | None = None
    recording_url: str | None = None
    browser_console_log_url: str | None = None
    downloaded_files: list[FileInfo] | None = None
    downloaded_file_urls: list[str] | None = None
    failure_reason: str | None = None
    errors: list[dict[str, Any]] = []
    max_steps_per_run: int | None = None
    workflow_run_id: str | None = None


class TaskOutput(BaseModel):
    task_id: str
    status: TaskStatus
    extracted_information: list | dict[str, Any] | str | None = None
    failure_reason: str | None = None
    errors: list[dict[str, Any]] = []
    downloaded_files: list[FileInfo] | None = None
    downloaded_file_urls: list[str] | None = None  # For backward compatibility

    @staticmethod
    def from_task(task: Task, downloaded_files: list[FileInfo] | None = None) -> TaskOutput:
        # For backward compatibility, extract just the URLs from FileInfo objects
        downloaded_file_urls = [file_info.url for file_info in downloaded_files] if downloaded_files else None

        return TaskOutput(
            task_id=task.task_id,
            status=task.status,
            extracted_information=task.extracted_information,
            failure_reason=task.failure_reason,
            errors=task.errors,
            downloaded_files=downloaded_files,
            downloaded_file_urls=downloaded_file_urls,
        )


class CreateTaskResponse(BaseModel):
    task_id: str


class OrderBy(StrEnum):
    created_at = "created_at"
    modified_at = "modified_at"


class SortDirection(StrEnum):
    asc = "asc"
    desc = "desc"
