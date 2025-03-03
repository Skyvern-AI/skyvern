from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel

from skyvern.forge.sdk.schemas.task_v2 import Thought
from skyvern.forge.sdk.workflow.models.block import BlockType
from skyvern.webeye.actions.actions import Action


class WorkflowRunBlock(BaseModel):
    workflow_run_block_id: str
    block_workflow_run_id: str | None = None
    workflow_run_id: str
    organization_id: str | None = None
    description: str | None = None
    parent_workflow_run_block_id: str | None = None
    block_type: BlockType
    label: str | None = None
    status: str | None = None
    output: dict | list | str | None = None
    continue_on_failure: bool = False
    failure_reason: str | None = None
    task_id: str | None = None
    url: str | None = None
    navigation_goal: str | None = None
    navigation_payload: dict[str, Any] | list | str | None = None
    data_extraction_goal: str | None = None
    data_schema: dict[str, Any] | list | str | None = None
    terminate_criterion: str | None = None
    complete_criterion: str | None = None
    actions: list[Action] = []
    created_at: datetime
    modified_at: datetime

    # for loop block
    loop_values: list[Any] | None = None

    # block inside a loop block
    current_value: str | None = None
    current_index: int | None = None

    # email block
    recipients: list[str] | None = None
    attachments: list[str] | None = None
    subject: str | None = None
    body: str | None = None


class WorkflowRunTimelineType(StrEnum):
    thought = "thought"
    block = "block"


class WorkflowRunTimeline(BaseModel):
    type: WorkflowRunTimelineType
    block: WorkflowRunBlock | None = None
    thought: Thought | None = None
    children: list[WorkflowRunTimeline] = []
    created_at: datetime
    modified_at: datetime
