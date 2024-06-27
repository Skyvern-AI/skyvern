from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
from skyvern.webeye.actions.actions import ActionType
from skyvern.webeye.actions.models import AgentStepOutput


class StepStatus(StrEnum):
    created = "created"
    running = "running"
    failed = "failed"
    completed = "completed"
    canceled = "canceled"

    def can_update_to(self, new_status: StepStatus) -> bool:
        allowed_transitions: dict[StepStatus, set[StepStatus]] = {
            StepStatus.created: {StepStatus.running, StepStatus.canceled},
            StepStatus.running: {StepStatus.completed, StepStatus.failed, StepStatus.canceled},
            StepStatus.failed: set(),
            StepStatus.completed: set(),
            StepStatus.canceled: set(),
        }
        return new_status in allowed_transitions[self]

    def requires_output(self) -> bool:
        status_requires_output = {StepStatus.completed}
        return self in status_requires_output

    def cant_have_output(self) -> bool:
        status_cant_have_output = {StepStatus.created, StepStatus.running}
        return self in status_cant_have_output

    def is_terminal(self) -> bool:
        status_is_terminal = {StepStatus.failed, StepStatus.completed, StepStatus.canceled}
        return self in status_is_terminal


class Step(BaseModel):
    created_at: datetime
    modified_at: datetime
    task_id: str
    step_id: str
    status: StepStatus
    output: AgentStepOutput | None = None
    order: int
    is_last: bool
    retry_index: int = 0
    organization_id: str | None = None
    input_token_count: int = 0
    output_token_count: int = 0
    step_cost: float = 0

    def validate_update(
        self,
        status: StepStatus | None,
        output: AgentStepOutput | None,
        is_last: bool | None,
    ) -> None:
        old_status = self.status

        if status and not old_status.can_update_to(status):
            raise ValueError(f"invalid_status_transition({old_status},{status},{self.step_id})")

        if status == StepStatus.canceled:
            return

        if status and status.requires_output() and output is None:
            raise ValueError(f"status_requires_output({status},{self.step_id})")

        if status and status.cant_have_output() and output is not None:
            raise ValueError(f"status_cant_have_output({status},{self.step_id})")

        if output is not None and status is None:
            raise ValueError(f"cant_set_output_without_updating_status({self.step_id})")

        if self.output is not None and output is not None:
            raise ValueError(f"cant_override_output({self.step_id})")

        if is_last and not self.status.is_terminal():
            raise ValueError(f"is_last_but_status_not_terminal({self.status},{self.step_id})")

        if is_last is False:
            raise ValueError(f"cant_set_is_last_to_false({self.step_id})")

    def is_goal_achieved(self) -> bool:
        if self.status != StepStatus.completed:
            return False
        # TODO (kerem): Remove this check once we have backfilled all the steps
        if self.output is None or self.output.actions_and_results is None:
            return False

        # Check if there is a successful complete action
        for action, action_results in self.output.actions_and_results:
            if action.action_type != ActionType.COMPLETE:
                continue

            if any(action_result.success for action_result in action_results):
                return True

        return False

    def is_terminated(self) -> bool:
        if self.status != StepStatus.completed:
            return False
        # TODO (kerem): Remove this check once we have backfilled all the steps
        if self.output is None or self.output.actions_and_results is None:
            return False

        # Check if there is a successful terminate action
        for action, action_results in self.output.actions_and_results:
            if action.action_type != ActionType.TERMINATE:
                continue

            if any(action_result.success for action_result in action_results):
                return True

        return False


class Organization(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    organization_id: str
    organization_name: str
    webhook_callback_url: str | None = None
    max_steps_per_run: int | None = None
    max_retries_per_step: int | None = None
    domain: str | None = None

    created_at: datetime
    modified_at: datetime


class OrganizationAuthToken(BaseModel):
    id: str
    organization_id: str
    token_type: OrganizationAuthTokenType
    token: str
    valid: bool
    created_at: datetime
    modified_at: datetime


class TokenPayload(BaseModel):
    sub: str
    exp: int
