from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from skyvern.exceptions import InvalidTaskStatusTransition, TaskAlreadyCanceled


class ProxyLocation(StrEnum):
    US_CA = "US-CA"
    US_NY = "US-NY"
    US_TX = "US-TX"
    US_FL = "US-FL"
    US_WA = "US-WA"
    RESIDENTIAL = "RESIDENTIAL"
    RESIDENTIAL_ES = "RESIDENTIAL_ES"
    NONE = "NONE"


class TaskRequest(BaseModel):
    title: str | None = Field(
        default=None,
        description="The title of the task.",
        examples=["Get a quote for car insurance"],
    )
    url: str = Field(
        ...,
        min_length=1,
        description="Starting URL for the task.",
        examples=["https://www.geico.com"],
    )
    # TODO: use HttpUrl instead of str
    webhook_callback_url: str | None = Field(
        default=None,
        description="The URL to call when the task is completed.",
        examples=["https://my-webhook.com"],
    )
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


class Task(TaskRequest):
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
            errors=self.errors,
        )


class TaskResponse(BaseModel):
    request: TaskRequest
    task_id: str
    status: TaskStatus
    created_at: datetime
    modified_at: datetime
    extracted_information: list | dict[str, Any] | str | None = None
    action_screenshot_urls: list[str] | None = None
    screenshot_url: str | None = None
    recording_url: str | None = None
    failure_reason: str | None = None
    errors: list[dict[str, Any]] = []


class TaskOutput(BaseModel):
    task_id: str
    status: TaskStatus
    extracted_information: list | dict[str, Any] | str | None = None
    failure_reason: str | None = None
    errors: list[dict[str, Any]] = []

    @staticmethod
    def from_task(task: Task) -> TaskOutput:
        return TaskOutput(
            task_id=task.task_id,
            status=task.status,
            extracted_information=task.extracted_information,
            failure_reason=task.failure_reason,
            errors=task.errors,
        )


class CreateTaskResponse(BaseModel):
    task_id: str
