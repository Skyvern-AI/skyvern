from __future__ import annotations

from typing import Any, Literal, cast

from pydantic import BaseModel, SerializerFunctionWrapHandler, model_serializer

from skyvern.errors.errors import UserDefinedError
from skyvern.utils.action_redaction import redact_action_for_log
from skyvern.webeye.actions.actions import Action
from skyvern.webeye.actions.responses import ActionResult


class BrowserMetadata(BaseModel):
    website_url: str | None = None


class StepToolCall(BaseModel):
    """One tool call the model made in a Task V3 turn. Names, outcomes and timings only: arguments and
    results carry end-user data."""

    tool: str
    # `refused`: the tool itself refused. `skipped`: the loop never dispatched it -- a budget, a cancellation, an
    # earlier failure in the same batch, or a refusal the loop answered before dispatch.
    status: Literal["ok", "error", "refused", "skipped"]
    duration_seconds: float | None = None
    billable: bool = False


class StepPageDom(BaseModel):
    """The page a Task V3 turn started from: stored in the turn's archive, the same as an earlier turn's stored page
    (`artifact_id` names that copy), or skipped for `reason`."""

    status: Literal["stored", "deduped", "skipped"]
    artifact_id: str | None = None
    reason: str | None = None


class AgentStepOutput(BaseModel):
    """
    Output of the agent step, this is recorded in the database.
    """

    # Will be deprecated once we move to the new format below
    action_results: list[ActionResult] | None = None
    # Nullable for backwards compatibility, once backfill is done, this won't be nullable anymore
    actions_and_results: list[tuple[Action, list[ActionResult]]] | None = None
    errors: list[UserDefinedError] = []
    # Explicit no-retry signal; historical/plain errors still use the normal retry budget.
    terminal_user_errors: bool = False
    browser_metadata: BrowserMetadata | None = None
    step_exception: str | None = None
    # Task V3 turn steps only: how long the turn's model call took (retries included) and what it called.
    llm_seconds: float | None = None
    tool_calls: list[StepToolCall] | None = None
    page_dom: StepPageDom | None = None

    @model_serializer(mode="wrap")
    def serialize_model(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        payload = cast(dict[str, Any], handler(self))
        if self.actions_and_results is None:
            return payload

        serialized_pairs = payload.get("actions_and_results") or []
        redacted_pairs = []
        for (action, _), (_, serialized_results) in zip(self.actions_and_results, serialized_pairs, strict=True):
            redacted_pairs.append((redact_action_for_log(action), serialized_results))
        payload["actions_and_results"] = redacted_pairs
        return payload

    def __repr__(self) -> str:
        return f"AgentStepOutput({self.model_dump()})"

    def __str__(self) -> str:
        return self.__repr__()
