from datetime import datetime
from enum import StrEnum
from typing import Any, List

from pydantic import BaseModel

from skyvern.forge.sdk.schemas.tasks import ProxyLocation
from skyvern.forge.sdk.workflow.exceptions import WorkflowDefinitionHasDuplicateBlockLabels
from skyvern.forge.sdk.workflow.models.block import BlockTypeVar
from skyvern.forge.sdk.workflow.models.parameter import PARAMETER_TYPE


class WorkflowRequestBody(BaseModel):
    data: dict[str, Any] | None = None
    proxy_location: ProxyLocation | None = None
    webhook_callback_url: str | None = None


class RunWorkflowResponse(BaseModel):
    workflow_id: str
    workflow_run_id: str


class WorkflowDefinition(BaseModel):
    parameters: list[PARAMETER_TYPE]
    blocks: List[BlockTypeVar]

    def validate(self) -> None:
        labels: set[str] = set()
        duplicate_labels: set[str] = set()
        for block in self.blocks:
            if block.label in labels:
                duplicate_labels.add(block.label)
            else:
                labels.add(block.label)

        if duplicate_labels:
            raise WorkflowDefinitionHasDuplicateBlockLabels(duplicate_labels)


class Workflow(BaseModel):
    workflow_id: str
    organization_id: str
    title: str
    workflow_permanent_id: str
    version: int
    is_saved_task: bool
    description: str | None = None
    workflow_definition: WorkflowDefinition
    proxy_location: ProxyLocation | None = None
    webhook_callback_url: str | None = None

    created_at: datetime
    modified_at: datetime
    deleted_at: datetime | None = None


class WorkflowRunStatus(StrEnum):
    created = "created"
    running = "running"
    failed = "failed"
    terminated = "terminated"
    completed = "completed"


class WorkflowRun(BaseModel):
    workflow_run_id: str
    workflow_id: str
    status: WorkflowRunStatus
    proxy_location: ProxyLocation | None = None
    webhook_callback_url: str | None = None

    created_at: datetime
    modified_at: datetime


class WorkflowRunParameter(BaseModel):
    workflow_run_id: str
    workflow_parameter_id: str
    value: bool | int | float | str | dict | list
    created_at: datetime


class WorkflowRunOutputParameter(BaseModel):
    workflow_run_id: str
    output_parameter_id: str
    value: dict[str, Any] | list | str | None
    created_at: datetime


class WorkflowRunStatusResponse(BaseModel):
    workflow_id: str
    workflow_run_id: str
    status: WorkflowRunStatus
    proxy_location: ProxyLocation | None = None
    webhook_callback_url: str | None = None
    created_at: datetime
    modified_at: datetime
    parameters: dict[str, Any]
    screenshot_urls: list[str] | None = None
    recording_url: str | None = None
    outputs: dict[str, Any] | None = None
