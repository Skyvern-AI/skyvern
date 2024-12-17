from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel

from skyvern.forge.sdk.schemas.observers import ObserverThought
from skyvern.forge.sdk.workflow.models.block import BlockType
from skyvern.webeye.actions.actions import Action


class WorkflowRunBlock(BaseModel):
    workflow_run_block_id: str = "placeholder"
    workflow_run_id: str
    parent_workflow_run_block_id: str | None = None
    block_type: BlockType
    label: str | None = None
    title: str | None = None
    status: str | None = None
    output: dict | list | str | None = None
    continue_on_failure: bool = False
    task_id: str | None = None
    url: str | None = None
    navigation_goal: str | None = None
    data_extraction_goal: str | None = None
    data_schema: dict[str, Any] | list | str | None = None
    terminate_criterion: str | None = None
    complete_criterion: str | None = None
    created_at: datetime
    modified_at: datetime


class WorkflowRunEventType(StrEnum):
    action = "action"
    thought = "thought"
    block = "block"


class WorkflowRunEvent(BaseModel):
    type: WorkflowRunEventType
    action: Action | None = None
    thought: ObserverThought | None = None
    block: WorkflowRunBlock | None = None
    created_at: datetime
    modified_at: datetime
