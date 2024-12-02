from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from skyvern.config import settings
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
    step_exception: str | None = None

    class Config:
        exclude = ["scraped_page", "extract_action_prompt"]

    def __repr__(self) -> str:
        if settings.DEBUG_MODE:
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

    def get_clean_detailed_output(self) -> DetailedAgentStepOutput:
        return DetailedAgentStepOutput(
            scraped_page=self.scraped_page,
            extract_action_prompt=self.extract_action_prompt,
            llm_response=self.llm_response,
            actions=self.actions,
            action_results=self.action_results,
            actions_and_results=None
            if self.actions_and_results is None
            else [(action, result) for action, result in self.actions_and_results if result],
            step_exception=self.step_exception,
        )

    def to_agent_step_output(self) -> AgentStepOutput:
        clean_output = self.get_clean_detailed_output()

        return AgentStepOutput(
            action_results=clean_output.action_results if clean_output.action_results else [],
            actions_and_results=(clean_output.actions_and_results if clean_output.actions_and_results else []),
            errors=clean_output.extract_errors(),
        )
