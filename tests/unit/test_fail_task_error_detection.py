"""
Unit tests for fail_task error detection integration.

Tests the integration between ForgeAgent.fail_task() and the error detection service.
"""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.errors.errors import UserDefinedError
from skyvern.forge.agent import ForgeAgent
from skyvern.forge.sdk.models import StepStatus
from skyvern.forge.sdk.schemas.tasks import TaskStatus
from skyvern.schemas.steps import AgentStepOutput
from skyvern.utils.secret_redaction import REDACTED_SECRET_PLACEHOLDER
from skyvern.webeye.actions.actions import Action, ActionType
from skyvern.webeye.actions.responses import ActionSuccess
from tests.unit.helpers import make_organization, make_step, make_task

_EXPECTED_500 = "An HTTP request made during the download action returned HTTP 500, and no file was received."


def _download_intent_action() -> Action:
    return Action(action_type=ActionType.CLICK, element_id="download-link", download=True)


def _failed_download_result(status: int) -> ActionSuccess:
    result = ActionSuccess()
    result.download_triggered = False
    result.download_failure_status = status
    return result


def _step_output(pairs: list) -> AgentStepOutput:
    return AgentStepOutput(action_results=[], actions_and_results=pairs)


def _echo_persisted_reason(base_task):
    """Mirror real update_task: return the persisted row so the caller reads back the failure_reason that
    was actually stored (post enrichment/redaction), not the stale local string it passed in."""

    def _update_task(task, status, failure_reason=None, **kwargs):
        return base_task.model_copy(update={"status": status, "failure_reason": failure_reason})

    return _update_task


@pytest.fixture
def agent():
    """Create a ForgeAgent instance."""
    return ForgeAgent()


@pytest.fixture
def mock_browser_state():
    """Create a mock browser state."""
    browser_state = MagicMock()
    page = MagicMock()
    page.url = "https://example.com/error"

    async def get_working_page():
        return page

    async def scrape_website(*args, **kwargs):
        return None

    browser_state.get_working_page = get_working_page
    browser_state.scrape_website = scrape_website
    return browser_state


@pytest.mark.asyncio
async def test_fail_task_with_error_code_mapping_detects_errors(agent, mock_browser_state):
    """Test that fail_task detects errors when error_code_mapping is provided."""
    now = datetime.now()
    organization = make_organization(now)
    task = make_task(
        now,
        organization,
        error_code_mapping={
            "payment_failed": "Payment was declined",
            "out_of_stock": "Product unavailable",
        },
    )
    step = make_step(now, task, step_id="step-1", status=StepStatus.running, order=1, output=None)

    detected_errors = [
        UserDefinedError(
            error_code="payment_failed", reasoning="Payment declined message shown on page", confidence_float=0.95
        )
    ]

    with patch.object(agent, "update_step", new_callable=AsyncMock):
        with patch.object(agent, "update_task", new_callable=AsyncMock) as mock_update_task:
            mock_update_task.side_effect = _echo_persisted_reason(task)

            with patch(
                "skyvern.forge.agent.detect_user_defined_errors_for_task",
                new_callable=AsyncMock,
            ) as mock_detect:
                mock_detect.return_value = detected_errors

                with patch("skyvern.forge.agent.app") as mock_app:
                    mock_app.DATABASE.tasks.update_task = AsyncMock()

                    result = await agent.fail_task(task, step, "Task failed", mock_browser_state)

                    assert result is True

                    # Verify error detection was called
                    mock_detect.assert_called_once_with(
                        task=task,
                        step=step,
                        browser_state=mock_browser_state,
                        failure_reason="Task failed",
                    )

                    # Verify task errors were updated in database
                    mock_app.DATABASE.tasks.update_task.assert_called_once()
                    call_kwargs = mock_app.DATABASE.tasks.update_task.call_args[1]
                    assert call_kwargs["task_id"] == task.task_id
                    assert call_kwargs["organization_id"] == task.organization_id
                    assert len(call_kwargs["errors"]) == 1
                    assert call_kwargs["errors"][0]["error_code"] == "payment_failed"


@pytest.mark.asyncio
async def test_fail_task_without_error_code_mapping(agent, mock_browser_state):
    """Test that fail_task skips detection when no error_code_mapping."""
    now = datetime.now()
    organization = make_organization(now)
    task = make_task(now, organization, error_code_mapping=None)
    step = make_step(now, task, step_id="step-1", status=StepStatus.running, order=1, output=None)

    with patch.object(agent, "update_step", new_callable=AsyncMock):
        with patch.object(agent, "update_task", new_callable=AsyncMock) as mock_update_task:
            mock_update_task.side_effect = _echo_persisted_reason(task)

            with patch(
                "skyvern.forge.agent.detect_user_defined_errors_for_task",
                new_callable=AsyncMock,
            ) as mock_detect:
                with patch("skyvern.forge.agent.app") as mock_app:
                    mock_app.DATABASE.tasks.update_task = AsyncMock()

                    result = await agent.fail_task(task, step, "Task failed", mock_browser_state)

                    assert result is True

                    # Verify error detection was NOT called
                    mock_detect.assert_not_called()

                    # Verify database update was NOT called for errors
                    mock_app.DATABASE.tasks.update_task.assert_not_called()


@pytest.mark.asyncio
async def test_fail_task_without_browser_state(agent):
    """Test that fail_task handles missing browser_state gracefully."""
    now = datetime.now()
    organization = make_organization(now)
    task = make_task(
        now,
        organization,
        error_code_mapping={
            "payment_failed": "Payment was declined",
        },
    )
    step = make_step(now, task, step_id="step-1", status=StepStatus.running, order=1, output=None)

    with patch.object(agent, "update_step", new_callable=AsyncMock):
        with patch.object(agent, "update_task", new_callable=AsyncMock) as mock_update_task:
            mock_update_task.side_effect = _echo_persisted_reason(task)

            with patch(
                "skyvern.forge.agent.detect_user_defined_errors_for_task",
                new_callable=AsyncMock,
            ) as mock_detect:
                mock_detect.return_value = []

                with patch("skyvern.forge.agent.app") as mock_app:
                    mock_app.DATABASE.tasks.update_task = AsyncMock()

                    # Call without browser_state
                    result = await agent.fail_task(task, step, "Task failed", browser_state=None)

                    assert result is True

                    # Error detection should still be called (will skip internally)
                    mock_detect.assert_called_once_with(
                        task=task,
                        step=step,
                        browser_state=None,
                        failure_reason="Task failed",
                    )


@pytest.mark.asyncio
async def test_fail_task_without_step(agent, mock_browser_state):
    """Test that fail_task handles missing step gracefully."""
    now = datetime.now()
    organization = make_organization(now)
    task = make_task(
        now,
        organization,
        error_code_mapping={
            "payment_failed": "Payment was declined",
        },
    )

    with patch.object(agent, "update_step", new_callable=AsyncMock) as mock_update_step:
        with patch.object(agent, "update_task", new_callable=AsyncMock) as mock_update_task:
            mock_update_task.side_effect = _echo_persisted_reason(task)

            with patch(
                "skyvern.forge.agent.detect_user_defined_errors_for_task",
                new_callable=AsyncMock,
            ) as mock_detect:
                with patch("skyvern.forge.agent.app") as mock_app:
                    mock_app.DATABASE.tasks.update_task = AsyncMock()

                    # Call without step
                    result = await agent.fail_task(task, None, "Task failed", mock_browser_state)

                    assert result is True

                    # Error detection should not be called (step is required)
                    mock_detect.assert_not_called()

                    # update_step should not be called
                    mock_update_step.assert_not_called()


@pytest.mark.asyncio
async def test_fail_task_preserves_completed_step_and_fails_task(agent, mock_browser_state):
    now = datetime.now()
    organization = make_organization(now)
    task = make_task(now, organization, error_code_mapping=None)
    step = make_step(now, task, step_id="step-1", status=StepStatus.completed, order=1, output=None)

    with patch.object(agent, "update_step", new_callable=AsyncMock) as mock_update_step:
        with patch.object(agent, "update_task", new_callable=AsyncMock) as mock_update_task:
            mock_update_task.side_effect = _echo_persisted_reason(task)

            result = await agent.fail_task(task, step, "Post-step billing failed", mock_browser_state)

            assert result is True
            mock_update_step.assert_not_awaited()
            mock_update_task.assert_awaited_once()
            assert mock_update_task.await_args.kwargs["status"] == TaskStatus.failed


@pytest.mark.asyncio
async def test_fail_task_error_detection_fails_gracefully(agent, mock_browser_state):
    """Test that fail_task continues even if error detection fails."""
    now = datetime.now()
    organization = make_organization(now)
    task = make_task(
        now,
        organization,
        error_code_mapping={
            "payment_failed": "Payment was declined",
        },
    )
    step = make_step(now, task, step_id="step-1", status=StepStatus.running, order=1, output=None)

    with patch.object(agent, "update_step", new_callable=AsyncMock):
        with patch.object(agent, "update_task", new_callable=AsyncMock) as mock_update_task:
            mock_update_task.side_effect = _echo_persisted_reason(task)

            with patch(
                "skyvern.forge.agent.detect_user_defined_errors_for_task",
                new_callable=AsyncMock,
            ) as mock_detect:
                # Error detection raises exception
                mock_detect.side_effect = Exception("Detection failed")

                with patch("skyvern.forge.agent.app") as mock_app:
                    mock_app.DATABASE.tasks.update_task = AsyncMock()

                    # Should not raise exception
                    result = await agent.fail_task(task, step, "Task failed", mock_browser_state)

                    # Task should still be marked as failed
                    assert result is True
                    mock_update_task.assert_called_once()


@pytest.mark.asyncio
async def test_fail_task_multiple_errors_detected(agent, mock_browser_state):
    """Test that fail_task handles multiple detected errors."""
    now = datetime.now()
    organization = make_organization(now)
    task = make_task(
        now,
        organization,
        error_code_mapping={
            "payment_failed": "Payment was declined",
            "address_invalid": "Address validation failed",
        },
        errors=[{"error_code": "existing_error", "reasoning": "Pre-existing error"}],
    )
    step = make_step(now, task, step_id="step-1", status=StepStatus.running, order=1, output=None)

    detected_errors = [
        UserDefinedError(error_code="payment_failed", reasoning="Payment declined", confidence_float=0.90),
        UserDefinedError(error_code="address_invalid", reasoning="Invalid shipping address", confidence_float=0.85),
    ]

    with patch.object(agent, "update_step", new_callable=AsyncMock):
        with patch.object(agent, "update_task", new_callable=AsyncMock) as mock_update_task:
            mock_update_task.side_effect = _echo_persisted_reason(task)

            with patch(
                "skyvern.forge.agent.detect_user_defined_errors_for_task",
                new_callable=AsyncMock,
            ) as mock_detect:
                mock_detect.return_value = detected_errors

                with patch("skyvern.forge.agent.app") as mock_app:
                    mock_app.DATABASE.tasks.update_task = AsyncMock()

                    result = await agent.fail_task(task, step, "Task failed", mock_browser_state)

                    assert result is True

                    # Verify only new errors were passed (DB handles appending to existing errors)
                    call_kwargs = mock_app.DATABASE.tasks.update_task.call_args[1]
                    assert len(call_kwargs["errors"]) == 2
                    assert call_kwargs["errors"][0]["error_code"] == "payment_failed"
                    assert call_kwargs["errors"][1]["error_code"] == "address_invalid"


@pytest.mark.asyncio
async def test_fail_task_no_errors_detected(agent, mock_browser_state):
    """Test that fail_task handles case where no errors are detected."""
    now = datetime.now()
    organization = make_organization(now)
    task = make_task(
        now,
        organization,
        error_code_mapping={
            "payment_failed": "Payment was declined",
        },
    )
    step = make_step(now, task, step_id="step-1", status=StepStatus.running, order=1, output=None)

    with patch.object(agent, "update_step", new_callable=AsyncMock):
        with patch.object(agent, "update_task", new_callable=AsyncMock) as mock_update_task:
            mock_update_task.side_effect = _echo_persisted_reason(task)

            with patch(
                "skyvern.forge.agent.detect_user_defined_errors_for_task",
                new_callable=AsyncMock,
            ) as mock_detect:
                mock_detect.return_value = []

                with patch("skyvern.forge.agent.app") as mock_app:
                    mock_app.DATABASE.tasks.update_task = AsyncMock()

                    result = await agent.fail_task(task, step, "Task failed", mock_browser_state)

                    assert result is True

                    # Database update for errors should not be called
                    mock_app.DATABASE.tasks.update_task.assert_not_called()


@pytest.mark.asyncio
async def test_fail_task_with_task_already_canceled(agent, mock_browser_state):
    """Test that fail_task returns False when task is already canceled."""
    now = datetime.now()
    organization = make_organization(now)
    task = make_task(
        now,
        organization,
        status=TaskStatus.canceled,
        error_code_mapping={
            "payment_failed": "Payment was declined",
        },
    )
    step = make_step(now, task, step_id="step-1", status=StepStatus.running, order=1, output=None)

    with patch.object(agent, "update_step", new_callable=AsyncMock):
        with patch.object(agent, "update_task", new_callable=AsyncMock) as mock_update_task:
            # Simulate TaskAlreadyCanceled exception
            from skyvern.exceptions import TaskAlreadyCanceled

            mock_update_task.side_effect = TaskAlreadyCanceled("new_status", task.task_id)

            with patch(
                "skyvern.forge.agent.detect_user_defined_errors_for_task",
                new_callable=AsyncMock,
            ) as mock_detect:
                result = await agent.fail_task(task, step, "Task failed", mock_browser_state)

                # Should return False
                assert result is False

                # Error detection should not be called
                mock_detect.assert_not_called()


@pytest.mark.asyncio
async def test_fail_task_with_task_already_timed_out(agent, mock_browser_state):
    """A reaper-timed-out task cannot be failed: return False, same as already-canceled."""
    now = datetime.now()
    organization = make_organization(now)
    task = make_task(
        now,
        organization,
        status=TaskStatus.timed_out,
        error_code_mapping=None,
    )
    step = make_step(now, task, step_id="step-1", status=StepStatus.running, order=1, output=None)

    with patch.object(agent, "update_step", new_callable=AsyncMock):
        with patch.object(agent, "update_task", new_callable=AsyncMock) as mock_update_task:
            from skyvern.exceptions import TaskAlreadyTimeout

            mock_update_task.side_effect = TaskAlreadyTimeout(task.task_id)

            result = await agent.fail_task(task, step, "Task failed", mock_browser_state)

            assert result is False


@pytest.mark.asyncio
async def test_fail_task_redacts_a_registered_secret_from_the_failure_reason(agent, mock_browser_state):
    """fail_task persists failure_reason and then webhooks the task itself, and this exit is
    reachable on v3 as well as v1 -- a run that dies by exception rather than by a model verdict
    lands here. Redaction happens once at the top, so the detector sees the redacted string too."""
    now = datetime.now()
    organization = make_organization(now)
    task = make_task(now, organization, error_code_mapping={"payment_failed": "Payment was declined"})
    step = make_step(now, task, step_id="step-1", status=StepStatus.running, order=1, output=None)

    with patch.object(agent, "update_step", new_callable=AsyncMock):
        with patch.object(agent, "update_task", new_callable=AsyncMock) as mock_update_task:
            mock_update_task.side_effect = _echo_persisted_reason(task)
            with patch(
                "skyvern.forge.agent.detect_user_defined_errors_for_task", new_callable=AsyncMock
            ) as mock_detect:
                mock_detect.return_value = []
                with patch("skyvern.forge.agent.app") as mock_app:
                    mock_app.WORKFLOW_CONTEXT_MANAGER.artifact_redaction_enabled.return_value = True
                    mock_app.WORKFLOW_CONTEXT_MANAGER.get_secret_values_for_run.return_value = {"sk4829137765"}
                    mock_app.DATABASE.tasks.update_task = AsyncMock()

                    await agent.fail_task(task, step, "the portal rejected the key sk4829137765", mock_browser_state)

    persisted = mock_update_task.await_args.kwargs["failure_reason"]
    assert "sk4829137765" not in persisted
    assert REDACTED_SECRET_PLACEHOLDER in persisted
    # Redacted at the top, so the detector is handed the scrubbed string rather than the raw one.
    assert "sk4829137765" not in mock_detect.await_args.kwargs["failure_reason"]


@pytest.mark.asyncio
async def test_fail_task_redacts_a_registered_secret_from_detector_written_errors(agent, mock_browser_state):
    """The detector reads the page as well as the failure reason, so redacting its input is not
    enough -- a secret typed into the form can come back in its reasoning, and task.errors ships
    over the same webhook."""
    now = datetime.now()
    organization = make_organization(now)
    task = make_task(now, organization, error_code_mapping={"payment_failed": "Payment was declined"})
    step = make_step(now, task, step_id="step-1", status=StepStatus.running, order=1, output=None)

    with patch.object(agent, "update_step", new_callable=AsyncMock):
        with patch.object(agent, "update_task", new_callable=AsyncMock) as mock_update_task:
            mock_update_task.side_effect = _echo_persisted_reason(task)
            with patch(
                "skyvern.forge.agent.detect_user_defined_errors_for_task", new_callable=AsyncMock
            ) as mock_detect:
                mock_detect.return_value = [
                    UserDefinedError(
                        error_code="payment_failed",
                        reasoning="the page showed sk4829137765 after submit",
                        confidence_float=1.0,
                    )
                ]
                with patch("skyvern.forge.agent.app") as mock_app:
                    mock_app.WORKFLOW_CONTEXT_MANAGER.artifact_redaction_enabled.return_value = True
                    mock_app.WORKFLOW_CONTEXT_MANAGER.get_secret_values_for_run.return_value = {"sk4829137765"}
                    mock_app.DATABASE.tasks.update_task = AsyncMock()

                    await agent.fail_task(task, step, "could not continue", mock_browser_state)

                    (persisted,) = mock_app.DATABASE.tasks.update_task.call_args[1]["errors"]

    assert "sk4829137765" not in persisted["reasoning"]
    assert REDACTED_SECRET_PLACEHOLDER in persisted["reasoning"]


@pytest.mark.asyncio
async def test_fail_task_leaves_the_failure_reason_alone_when_no_secrets_are_registered(agent, mock_browser_state):
    """Secret values are available only through the gate; with the gate off the scrub must not run
    at all, so a run that never opted in gets its text back unchanged."""
    now = datetime.now()
    organization = make_organization(now)
    task = make_task(now, organization, error_code_mapping={"payment_failed": "Payment was declined"})
    step = make_step(now, task, step_id="step-1", status=StepStatus.running, order=1, output=None)
    raw = "the portal rejected the key sk4829137765"

    with patch.object(agent, "update_step", new_callable=AsyncMock):
        with patch.object(agent, "update_task", new_callable=AsyncMock) as mock_update_task:
            mock_update_task.side_effect = _echo_persisted_reason(task)
            with patch(
                "skyvern.forge.agent.detect_user_defined_errors_for_task", new_callable=AsyncMock
            ) as mock_detect:
                mock_detect.return_value = []
                with patch("skyvern.forge.agent.app") as mock_app:
                    mock_app.WORKFLOW_CONTEXT_MANAGER.artifact_redaction_enabled.return_value = False
                    mock_app.WORKFLOW_CONTEXT_MANAGER.get_secret_values_for_run.return_value = {"sk4829137765"}
                    mock_app.WORKFLOW_CONTEXT_MANAGER.runtime_secret_values_for_artifacts.return_value = set()
                    mock_app.DATABASE.tasks.update_task = AsyncMock()

                    await agent.fail_task(task, step, raw, mock_browser_state)

    assert mock_update_task.await_args.kwargs["failure_reason"] == raw


@pytest.mark.asyncio
async def test_detector_receives_the_enriched_failure_reason_persisted_by_update_task(agent, mock_browser_state):
    """The download 5xx enrichment is stamped inside update_task at the persistence seam, so the string
    fail_task computed locally is stale by the time the row is written. The detector must be handed the
    post-update persisted reason -- the one carrying the HTTP 5xx/no-file sentence -- so its judgment sees
    the same evidence that ships to the customer. Drives real fail_task -> real update_task -> real
    _enrich_failure_reason_with_download_status, mocking only DB/browser/LLM/telemetry boundaries."""
    now = datetime.now()
    organization = make_organization(now)
    task = make_task(
        now,
        organization,
        task_id="task-dl",
        workflow_run_id="wr-dl",
        error_code_mapping={"download_failed": "The document could not be downloaded"},
    )
    step = make_step(
        now,
        task,
        step_id="step-1",
        status=StepStatus.completed,
        order=0,
        output=_step_output([(_download_intent_action(), [_failed_download_result(500)])]),
    )
    reason = "The task failed while attempting to retrieve the document."

    def _claim(task_id, organization_id, **updates):
        persisted = task.model_copy(
            update={"status": updates.get("status"), "failure_reason": updates.get("failure_reason")}
        )
        return persisted, False

    claim = AsyncMock(side_effect=_claim)

    with patch.object(agent, "update_step", new_callable=AsyncMock):
        with patch("skyvern.forge.agent.detect_user_defined_errors_for_task", new_callable=AsyncMock) as mock_detect:
            mock_detect.return_value = []
            with (
                patch("skyvern.forge.agent.app") as mock_app,
                patch("skyvern.forge.agent.save_task_logs", new_callable=AsyncMock),
            ):
                mock_app.WORKFLOW_CONTEXT_MANAGER.artifact_redaction_enabled.return_value = False
                mock_app.WORKFLOW_CONTEXT_MANAGER.runtime_secret_values_for_artifacts.return_value = set()
                mock_app.DATABASE.tasks.get_task = AsyncMock(return_value=task)
                mock_app.DATABASE.tasks.get_task_steps = AsyncMock(return_value=[step])
                mock_app.DATABASE.tasks.update_task_and_claim_finish = claim
                mock_app.DATABASE.tasks.update_task = AsyncMock()
                mock_app.DATABASE.observer.get_workflow_run_block_engine_by_task_id = AsyncMock(return_value=None)
                mock_app.AGENT_FUNCTION.record_run_duration = AsyncMock()
                mock_app.AGENT_FUNCTION.on_task_completed = AsyncMock()

                result = await agent.fail_task(task, step, reason, mock_browser_state)

    assert result is True
    persisted_reason = claim.await_args.kwargs["failure_reason"]
    assert persisted_reason == f"{reason} {_EXPECTED_500}"
    detector_reason = mock_detect.await_args.kwargs["failure_reason"]
    assert _EXPECTED_500 in detector_reason
    assert detector_reason == persisted_reason


@pytest.mark.asyncio
async def test_reason_none_stays_none_even_when_updated_task_carries_a_stale_reason(agent, mock_browser_state):
    """When the caller's reason is None the detector must receive None, even though update_task returns a
    persisted Task whose failure_reason column still holds an older value. fail_task keys the detector
    input off the original reason, not the returned row -- so a None reason never picks up a stale string."""
    now = datetime.now()
    organization = make_organization(now)
    task = make_task(now, organization, error_code_mapping={"download_failed": "The document could not be downloaded"})
    step = make_step(now, task, step_id="step-1", status=StepStatus.running, order=0, output=None)
    stale = make_task(now, organization, failure_reason="a stale failure reason from a previous write")

    with patch.object(agent, "update_step", new_callable=AsyncMock):
        with patch.object(agent, "update_task", new_callable=AsyncMock) as mock_update_task:
            mock_update_task.return_value = stale
            with patch(
                "skyvern.forge.agent.detect_user_defined_errors_for_task", new_callable=AsyncMock
            ) as mock_detect:
                mock_detect.return_value = []
                with patch("skyvern.forge.agent.app") as mock_app:
                    mock_app.DATABASE.tasks.update_task = AsyncMock()

                    result = await agent.fail_task(task, step, None, mock_browser_state)

    assert result is True
    assert mock_detect.await_args.kwargs["failure_reason"] is None
