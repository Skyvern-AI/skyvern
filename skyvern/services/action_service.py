from typing import Any

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.models import Step
from skyvern.forge.sdk.schemas.tasks import Task
from skyvern.webeye.actions.actions import Action
from skyvern.webeye.actions.responses import ActionResult


async def get_action_history(
    task: Task, current_step: Step | None = None, history_window: int = settings.PROMPT_ACTION_HISTORY_WINDOW
) -> list[dict[str, Any]]:
    """
    Get the action results from the last history_window steps.
    If current_step is provided, the current executing step will be included in the action history.
    Default is excluding the current executing step from the action history.
    """

    # Get action results from the last history_window steps
    steps = await app.DATABASE.get_task_steps(task_id=task.task_id, organization_id=task.organization_id)
    # the last step is always the newly created one and it should be excluded from the history window
    window_steps = steps[-1 - history_window : -1]
    if current_step:
        window_steps.append(current_step)

    actions_and_results: list[tuple[Action, list[ActionResult]]] = []
    for window_step in window_steps:
        if window_step.output and window_step.output.actions_and_results:
            actions_and_results.extend(window_step.output.actions_and_results)

    # exclude successful action from history
    action_history = [
        {
            "action": action.model_dump(
                exclude_none=True,
                include={"action_type", "element_id", "status", "reasoning", "option", "download"},
            ),
            # use the last result of the action, because some actions(like chain_click)
            # might have multiple results. Only the last one can represent the real result,
            # the previous results will be all failed
            "result": results[-1].model_dump(
                exclude_none=True,
                include={
                    "success",
                    "exception_type",
                    "exception_message",
                    "download_triggered",
                },
            ),
        }
        for action, results in actions_and_results
        if len(results) > 0
    ]
    return action_history
