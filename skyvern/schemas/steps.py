from __future__ import annotations

from pydantic import BaseModel

from skyvern.errors.errors import UserDefinedError
from skyvern.webeye.actions.actions import Action
from skyvern.webeye.actions.responses import ActionResult


class AgentStepOutput(BaseModel):
    """
    Output of the agent step, this is recorded in the database.
    """

    # Will be deprecated once we move to the new format below
    action_results: list[ActionResult] | None = None
    # Nullable for backwards compatibility, once backfill is done, this won't be nullable anymore
    actions_and_results: list[tuple[Action, list[ActionResult]]] | None = None
    errors: list[UserDefinedError] = []

    def __repr__(self) -> str:
        return f"AgentStepOutput({self.model_dump()})"

    def __str__(self) -> str:
        return self.__repr__()
