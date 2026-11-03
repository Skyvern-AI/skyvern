"""update_task enrichment: a terminal failure reason gains one bounded sentence when the run's latest
download-intent action received no file and passively observed a server 5xx.

The enrichment lives at the shared ForgeAgent.update_task persistence seam, so every terminal path --
fail_task, the max-steps terminate path, task_v3 -- inherits it. It is exception-agnostic and
page-agnostic: only the persisted download evidence folded across the run's steps decides, and only the
stored failure_reason changes -- never the classification.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.forge.agent import ForgeAgent
from skyvern.forge.sdk.models import StepStatus
from skyvern.forge.sdk.schemas.tasks import TaskStatus
from skyvern.schemas.steps import AgentStepOutput
from skyvern.webeye.actions.actions import Action, ActionType
from skyvern.webeye.actions.responses import ActionSuccess
from tests.unit.helpers import make_organization, make_step, make_task

_BLANK_PAGE_REASON = "Skyvern failed to load the website. The page may have become unresponsive during analysis."
_MAX_STEPS_REASON = "Skyvern reached the maximum number of steps allowed for this task."
_GENERIC_FAILED_REASON = "The task failed while attempting to retrieve the document."


def _download_intent_action() -> Action:
    return Action(action_type=ActionType.CLICK, element_id="download-link", download=True)


def _non_download_action() -> Action:
    return Action(action_type=ActionType.CLICK, element_id="menu")


def _failed_download_result(status: int) -> ActionSuccess:
    result = ActionSuccess()
    result.download_triggered = False
    result.download_failure_status = status
    return result


def _failed_download_result_without_status() -> ActionSuccess:
    result = ActionSuccess()
    result.download_triggered = False
    return result


def _successful_download_result() -> ActionSuccess:
    result = ActionSuccess()
    result.download_triggered = True
    result.downloaded_files = ["statement.pdf"]
    return result


def _saved_file_download_result_with_status(status: int) -> ActionSuccess:
    result = ActionSuccess()
    result.download_triggered = True
    result.downloaded_files = ["statement.pdf"]
    result.download_failure_status = status
    return result


def _triggered_empty_download_result(status: int) -> ActionSuccess:
    result = ActionSuccess()
    result.download_triggered = True
    result.download_failure_status = status
    return result


def _triggered_empty_download_result_without_status() -> ActionSuccess:
    result = ActionSuccess()
    result.download_triggered = True
    return result


def _ordinary_result() -> ActionSuccess:
    return ActionSuccess()


def _step_output(pairs: list[tuple[Action, list]]) -> AgentStepOutput:
    return AgentStepOutput(action_results=[], actions_and_results=pairs)


async def _run_update_task(
    *,
    step_specs: list[tuple[StepStatus, list[tuple[Action, list]] | None]],
    status: TaskStatus = TaskStatus.failed,
    reason: str | None = _GENERIC_FAILED_REASON,
    extracted_information: str | None = None,
    failure_category: list[dict] | None = None,
    get_task_steps_side_effect: Exception | None = None,
) -> tuple[AsyncMock, MagicMock]:
    now = datetime.now(UTC)
    org = make_organization(now)
    task = make_task(now, org, task_id="task-dl", workflow_run_id="wr-dl")
    updated = make_task(
        now,
        org,
        task_id="task-dl",
        workflow_run_id="wr-dl",
        status=status,
        failure_reason=reason,
        extracted_information=extracted_information,
    )

    steps = []
    for index, (step_status, pairs) in enumerate(step_specs):
        output = _step_output(pairs) if pairs is not None else None
        steps.append(make_step(now, task, step_id=f"step-{index}", status=step_status, order=index, output=output))

    agent = ForgeAgent()
    mock_app = MagicMock()
    mock_app.DATABASE.tasks.get_task = AsyncMock(return_value=task)
    if get_task_steps_side_effect is not None:
        mock_app.DATABASE.tasks.get_task_steps = AsyncMock(side_effect=get_task_steps_side_effect)
    else:
        mock_app.DATABASE.tasks.get_task_steps = AsyncMock(return_value=steps)
    claim = AsyncMock(return_value=(updated, False))
    mock_app.DATABASE.tasks.update_task_and_claim_finish = claim
    mock_app.DATABASE.tasks.get_workflow_run_block_engine_by_task_id = AsyncMock(return_value=None)
    mock_app.AGENT_FUNCTION.record_run_duration = AsyncMock()
    mock_app.AGENT_FUNCTION.on_task_completed = AsyncMock()

    with patch("skyvern.forge.agent.app", mock_app), patch("skyvern.forge.agent.save_task_logs", AsyncMock()):
        await agent.update_task(
            task,
            status=status,
            extracted_information=extracted_information,
            failure_reason=reason,
            failure_category=failure_category,
        )
    return claim, mock_app


def _stored_reason(claim: AsyncMock) -> str | None:
    return claim.await_args.kwargs.get("failure_reason")


_EXPECTED_500 = "An HTTP request made during the download action returned HTTP 500, and no file was received."


@pytest.mark.asyncio
async def test_blank_page_reason_with_5xx_is_still_enriched() -> None:
    claim, _ = await _run_update_task(
        step_specs=[(StepStatus.completed, [(_download_intent_action(), [_failed_download_result(500)])])],
        reason=_BLANK_PAGE_REASON,
    )
    assert _stored_reason(claim) == f"{_BLANK_PAGE_REASON} {_EXPECTED_500}"


@pytest.mark.asyncio
async def test_non_blank_failed_reason_with_latest_5xx_is_enriched() -> None:
    claim, _ = await _run_update_task(
        step_specs=[(StepStatus.completed, [(_download_intent_action(), [_failed_download_result(500)])])],
        status=TaskStatus.failed,
        reason=_GENERIC_FAILED_REASON,
    )
    assert _stored_reason(claim) == f"{_GENERIC_FAILED_REASON} {_EXPECTED_500}"


@pytest.mark.asyncio
async def test_terminated_after_ordinary_non_download_step_is_enriched() -> None:
    claim, _ = await _run_update_task(
        step_specs=[
            (StepStatus.completed, [(_download_intent_action(), [_failed_download_result(500)])]),
            (StepStatus.completed, [(_non_download_action(), [_ordinary_result()])]),
        ],
        status=TaskStatus.terminated,
        reason=_MAX_STEPS_REASON,
    )
    assert _stored_reason(claim) == f"{_MAX_STEPS_REASON} {_EXPECTED_500}"


@pytest.mark.asyncio
async def test_evidence_on_a_failed_step_is_enriched() -> None:
    claim, _ = await _run_update_task(
        step_specs=[(StepStatus.failed, [(_download_intent_action(), [_failed_download_result(500)])])],
        status=TaskStatus.failed,
    )
    assert _stored_reason(claim) == f"{_GENERIC_FAILED_REASON} {_EXPECTED_500}"


@pytest.mark.asyncio
@pytest.mark.parametrize("http_status", [500, 502, 503, 504])
async def test_status_sentence_uses_actual_status(http_status: int) -> None:
    claim, _ = await _run_update_task(
        step_specs=[(StepStatus.completed, [(_download_intent_action(), [_failed_download_result(http_status)])])],
    )
    assert f"HTTP {http_status}" in _stored_reason(claim)


@pytest.mark.asyncio
async def test_later_successful_download_clears_earlier_status() -> None:
    claim, _ = await _run_update_task(
        step_specs=[
            (StepStatus.completed, [(_download_intent_action(), [_failed_download_result(500)])]),
            (StepStatus.completed, [(_download_intent_action(), [_successful_download_result()])]),
        ],
    )
    assert _stored_reason(claim) == _GENERIC_FAILED_REASON


@pytest.mark.asyncio
async def test_saved_file_with_status_clears_status() -> None:
    # A saved file is ground truth: the latest download-intent action produced a non-empty
    # ``downloaded_files``, so its stamped status is ignored and the terminal reason is left unchanged.
    claim, _ = await _run_update_task(
        step_specs=[
            (StepStatus.completed, [(_download_intent_action(), [_saved_file_download_result_with_status(500)])])
        ],
    )
    assert _stored_reason(claim) == _GENERIC_FAILED_REASON


@pytest.mark.asyncio
async def test_triggered_but_empty_download_with_status_survives() -> None:
    # download_triggered=True is only a credited signal, not proof a file was saved. A credited-but-empty
    # action that carries a status keeps it, so the terminal reason is still enriched.
    claim, _ = await _run_update_task(
        step_specs=[(StepStatus.completed, [(_download_intent_action(), [_triggered_empty_download_result(500)])])],
    )
    assert _stored_reason(claim) == f"{_GENERIC_FAILED_REASON} {_EXPECTED_500}"


@pytest.mark.asyncio
async def test_later_triggered_but_empty_no_status_clears_earlier_status() -> None:
    # A later credited-but-empty action that observed no status is fresh evidence of no file, so it
    # clears the earlier stamped status even though download_triggered is True.
    claim, _ = await _run_update_task(
        step_specs=[
            (StepStatus.completed, [(_download_intent_action(), [_failed_download_result(500)])]),
            (StepStatus.completed, [(_download_intent_action(), [_triggered_empty_download_result_without_status()])]),
        ],
    )
    assert _stored_reason(claim) == _GENERIC_FAILED_REASON


@pytest.mark.asyncio
async def test_later_no_status_failed_download_clears_earlier_status() -> None:
    claim, _ = await _run_update_task(
        step_specs=[
            (StepStatus.completed, [(_download_intent_action(), [_failed_download_result(500)])]),
            (StepStatus.completed, [(_download_intent_action(), [_failed_download_result_without_status()])]),
        ],
    )
    assert _stored_reason(claim) == _GENERIC_FAILED_REASON


@pytest.mark.asyncio
async def test_ordinary_action_after_evidence_does_not_clear_it() -> None:
    claim, _ = await _run_update_task(
        step_specs=[
            (
                StepStatus.completed,
                [
                    (_download_intent_action(), [_failed_download_result(503)]),
                    (_non_download_action(), [_ordinary_result()]),
                ],
            ),
        ],
    )
    assert "HTTP 503" in _stored_reason(claim)


@pytest.mark.asyncio
async def test_successful_task_is_never_enriched() -> None:
    _, mock_app = await _run_update_task(
        step_specs=[(StepStatus.completed, [(_download_intent_action(), [_failed_download_result(500)])])],
        status=TaskStatus.completed,
        reason=None,
        extracted_information="the extracted quote",
    )
    mock_app.DATABASE.tasks.get_task_steps.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_steps_leaves_reason_unchanged() -> None:
    claim, _ = await _run_update_task(step_specs=[])
    assert _stored_reason(claim) == _GENERIC_FAILED_REASON


@pytest.mark.asyncio
async def test_no_download_evidence_leaves_reason_unchanged() -> None:
    claim, _ = await _run_update_task(
        step_specs=[(StepStatus.completed, [(_non_download_action(), [_ordinary_result()])])],
    )
    assert _stored_reason(claim) == _GENERIC_FAILED_REASON


@pytest.mark.asyncio
async def test_sentence_is_not_appended_twice() -> None:
    already = f"{_GENERIC_FAILED_REASON} {_EXPECTED_500}"
    claim, _ = await _run_update_task(
        step_specs=[(StepStatus.completed, [(_download_intent_action(), [_failed_download_result(500)])])],
        reason=already,
    )
    stored = _stored_reason(claim)
    assert stored == already
    assert stored.count("no file was received") == 1


@pytest.mark.asyncio
async def test_db_lookup_failure_leaves_reason_unchanged() -> None:
    claim, _ = await _run_update_task(
        step_specs=[(StepStatus.completed, [(_download_intent_action(), [_failed_download_result(500)])])],
        get_task_steps_side_effect=RuntimeError("db down"),
    )
    assert _stored_reason(claim) == _GENERIC_FAILED_REASON


@pytest.mark.asyncio
async def test_classification_is_passed_through_unchanged() -> None:
    category = [{"category": "download_failure", "reasoning": "example"}]
    claim, _ = await _run_update_task(
        step_specs=[(StepStatus.completed, [(_download_intent_action(), [_failed_download_result(500)])])],
        failure_category=category,
    )
    assert claim.await_args.kwargs.get("failure_category") == category


def test_status_evidence_survives_step_output_serialization() -> None:
    output = _step_output([(_download_intent_action(), [_failed_download_result(500)])])
    rehydrated = AgentStepOutput.model_validate(output.model_dump())
    _, results = rehydrated.actions_and_results[0]
    assert results[0].download_failure_status == 500
