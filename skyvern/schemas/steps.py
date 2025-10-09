from __future__ import annotations

from enum import StrEnum
from typing import List

from pydantic import BaseModel, Field

from skyvern.errors.errors import UserDefinedError
from skyvern.webeye.actions.actions import Action
from skyvern.webeye.actions.responses import ActionResult


class StrategyOutcome(StrEnum):
    failed = "failed"
    succeeded = "succeeded"
    blocked = "blocked"


class TriedStrategy(BaseModel):
    label: str
    summary: str
    evidence: str
    outcome: StrategyOutcome
    signals: List[str] = Field(default_factory=list)
    attempts: int
    last_seen_state_fingerprint: str


class AgentStepOutput(BaseModel):
    """
    Output of the agent step, this is recorded in the database.
    """

    # Will be deprecated once we move to the new format below
    action_results: list[ActionResult] | None = None
    # Nullable for backwards compatibility, once backfill is done, this won't be nullable anymore
    actions_and_results: list[tuple[Action, list[ActionResult]]] | None = None
    errors: list[UserDefinedError] = []
    tried_strategies: list[TriedStrategy] = Field(default_factory=list)

    def __repr__(self) -> str:
        return f"AgentStepOutput({self.model_dump()})"

    def __str__(self) -> str:
        return self.__repr__()
