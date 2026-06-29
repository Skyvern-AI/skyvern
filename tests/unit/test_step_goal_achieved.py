"""Step.is_goal_achieved must not let a planner-emitted mid-task EXTRACT complete a navigation task."""

from datetime import datetime, timezone

from skyvern.forge.sdk.models import Step, StepStatus
from skyvern.schemas.steps import AgentStepOutput
from skyvern.webeye.actions.actions import Action, ClickAction, CompleteAction, ExtractAction
from skyvern.webeye.actions.responses import ActionFailure, ActionResult, ActionSuccess


def _step(actions_and_results: list[tuple[Action, list[ActionResult]]]) -> Step:
    return Step(
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        modified_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        task_id="tsk_1",
        step_id="stp_1",
        status=StepStatus.completed,
        output=AgentStepOutput(actions_and_results=actions_and_results),
        order=0,
        is_last=False,
        organization_id="o_1",
    )


def test_trailing_complete_achieves_goal_with_navigation_goal() -> None:
    step = _step([(CompleteAction(reasoning="done"), [ActionSuccess()])])
    assert step.is_goal_achieved(has_navigation_goal=True) is True
    assert step.is_goal_achieved(has_navigation_goal=False) is True


def test_trailing_extract_alone_does_not_achieve_goal_with_navigation_goal() -> None:
    step = _step([(ExtractAction(reasoning="extract"), [ActionSuccess(data={"price": "$1"})])])
    assert step.is_goal_achieved(has_navigation_goal=True) is False


def test_trailing_extract_achieves_goal_without_navigation_goal() -> None:
    step = _step([(ExtractAction(reasoning="extract"), [ActionSuccess(data={"price": "$1"})])])
    assert step.is_goal_achieved(has_navigation_goal=False) is True


def test_extract_appended_after_successful_complete_achieves_goal() -> None:
    step = _step(
        [
            (CompleteAction(reasoning="done"), [ActionSuccess()]),
            (ExtractAction(reasoning="extract"), [ActionSuccess(data={"price": "$1"})]),
        ]
    )
    assert step.is_goal_achieved(has_navigation_goal=True) is True


def test_trailing_extract_after_failed_complete_does_not_achieve_goal() -> None:
    step = _step(
        [
            (CompleteAction(reasoning="done"), [ActionFailure(exception=Exception("not achieved"))]),
            (ExtractAction(reasoning="extract"), [ActionSuccess(data={"price": "$1"})]),
        ]
    )
    assert step.is_goal_achieved(has_navigation_goal=True) is False


def test_trailing_web_action_does_not_achieve_goal() -> None:
    step = _step([(ClickAction(reasoning="click", element_id="1"), [ActionSuccess()])])
    assert step.is_goal_achieved(has_navigation_goal=False) is False


def test_failed_trailing_extract_does_not_achieve_goal() -> None:
    step = _step([(ExtractAction(reasoning="extract"), [ActionFailure(exception=Exception("no data"))])])
    assert step.is_goal_achieved(has_navigation_goal=False) is False
