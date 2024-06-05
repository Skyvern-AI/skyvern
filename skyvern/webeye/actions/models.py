from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from skyvern.forge.sdk.settings_manager import SettingsManager
from skyvern.webeye.actions.actions import Action, DecisiveAction, UserDefinedError
from skyvern.webeye.actions.responses import ActionResult
from skyvern.webeye.scraper.scraper import ScrapedPage


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


class DetailedAgentStepOutput(BaseModel):
    """
    Output of the agent step, this is not recorded in the database, only used for debugging in the Jupyter notebook.
    """

    scraped_page: ScrapedPage | None
    extract_action_prompt: str | None
    llm_response: dict[str, Any] | None
    actions: list[Action] | None
    action_results: list[ActionResult] | None
    actions_and_results: list[tuple[Action, list[ActionResult]]] | None

    class Config:
        exclude = ["scraped_page", "extract_action_prompt"]

    def __repr__(self) -> str:
        if SettingsManager.get_settings().DEBUG_MODE:
            return f"DetailedAgentStepOutput({self.model_dump()})"
        else:
            return f"AgentStepOutput({self.to_agent_step_output().model_dump()})"

    def __str__(self) -> str:
        return self.__repr__()

    def extract_errors(self) -> list[UserDefinedError]:
        errors = []
        if self.actions_and_results:
            for action, action_results in self.actions_and_results:
                if isinstance(action, DecisiveAction):
                    errors.extend(action.errors)
        return errors

    def to_agent_step_output(self) -> AgentStepOutput:
        return AgentStepOutput(
            action_results=self.action_results if self.action_results else [],
            actions_and_results=(self.actions_and_results if self.actions_and_results else []),
            errors=self.extract_errors(),
        )
