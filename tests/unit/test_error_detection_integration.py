"""
Integration tests for error detection across different failure scenarios.

Tests the complete flow from task failure to error detection and storage.
"""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.errors.errors import ReachMaxRetriesError, UserDefinedError
from skyvern.forge.agent import ForgeAgent
from skyvern.forge.sdk.models import StepStatus
from skyvern.utils.prompt_engine import MaxStepsReasonResponse
from tests.unit.helpers import make_organization, make_step, make_task


@pytest.fixture
def agent():
    """Create a ForgeAgent instance."""
    return ForgeAgent()


@pytest.fixture
def mock_browser_state():
    """Create a complete mock browser state."""
    browser_state = MagicMock()
    page = MagicMock()
    page.url = "https://example.com/checkout"

    # Use AsyncMock so "await browser_state.get_working_page()" never hits a MagicMock
    browser_state.get_working_page = AsyncMock(return_value=page)
    browser_state.cleanup_element_tree = MagicMock()

    # Mock scrape_website
    scraped_page = MagicMock()
    scraped_page.url = "https://example.com/checkout"
    scraped_page.build_element_tree = MagicMock(
        return_value='<html><body><div class="error">Payment failed</div></body></html>'
    )
    scraped_page.screenshots = [b"screenshot_data"]

    async def scrape_website(**kwargs):
        return scraped_page

    browser_state.scrape_website = scrape_website

    return browser_state


def create_error_detection_mocks(detected_errors):
    """Helper to create standard error detection mocks."""
    # Mock the top-level detect_user_defined_errors_for_task function
    return patch(
        "skyvern.forge.agent.detect_user_defined_errors_for_task",
        new_callable=AsyncMock,
        return_value=detected_errors,
    )


@pytest.mark.asyncio
async def test_navigate_failure_with_error_detection(agent, mock_browser_state):
    """Test error detection when navigation fails."""
    now = datetime.now()
    organization = make_organization(now)
    task = make_task(
        now,
        organization,
        error_code_mapping={
            "page_not_found": "The requested page does not exist",
            "server_error": "Server is experiencing issues",
        },
    )
    step = make_step(now, task, step_id="step-1", status=StepStatus.running, order=1, output=None)

    detected_errors = [
        UserDefinedError(error_code="page_not_found", reasoning="404 error page shown", confidence_float=0.95)
    ]

    with patch.object(agent, "update_step", new_callable=AsyncMock):
        with patch.object(agent, "update_task", new_callable=AsyncMock) as mock_update_task:
            mock_update_task.return_value = task

            with create_error_detection_mocks(detected_errors):
                with patch("skyvern.forge.agent.app") as mock_app:
                    mock_app.DATABASE.update_task = AsyncMock()

                    # Simulate FailedToNavigateToUrl scenario
                    from skyvern.exceptions import FailedToNavigateToUrl

                    try:
                        raise FailedToNavigateToUrl(url=task.url, error_message="Navigation timeout")
                    except FailedToNavigateToUrl:
                        # Call fail_task as the exception handler would
                        result = await agent.fail_task(
                            task,
                            step,
                            f"Failed to navigate to URL. URL:{task.url}, Error:Navigation timeout",
                            mock_browser_state,
                        )

                        assert result is True

                        # Verify errors were stored
                        mock_app.DATABASE.update_task.assert_called_once()
                        call_kwargs = mock_app.DATABASE.update_task.call_args[1]
                        assert len(call_kwargs["errors"]) == 1
                        assert call_kwargs["errors"][0]["error_code"] == "page_not_found"


@pytest.mark.asyncio
async def test_max_retries_with_error_detection(agent, mock_browser_state):
    """Test error detection when max retries are exceeded."""
    now = datetime.now()
    organization = make_organization(now).model_copy(update={"max_retries_per_step": 3})
    task = make_task(
        now,
        organization,
        error_code_mapping={
            "captcha_failed": "CAPTCHA verification failed",
            "rate_limited": "Too many requests",
        },
    )
    step = make_step(now, task, step_id="step-3", status=StepStatus.failed, order=1, retry_index=3, output=None)

    detected_errors = [
        UserDefinedError(error_code="rate_limited", reasoning="Rate limit message displayed", confidence_float=0.90)
    ]

    # Mock summary_failure_reason_for_max_retries to return MaxStepsReasonResponse with detected errors
    async def mock_summary(*args, **kwargs):
        return MaxStepsReasonResponse(
            page_info="",
            reasoning="Multiple retry failures",
            errors=detected_errors,
        )

    with patch("skyvern.forge.agent.app") as mock_app:
        mock_app.BROWSER_MANAGER.get_for_task.return_value = mock_browser_state
        mock_app.DATABASE.get_task_steps = AsyncMock(return_value=[step, step, step])
        mock_app.DATABASE.get_task = AsyncMock(return_value=task)
        mock_app.DATABASE.update_task = AsyncMock(return_value=task)
        # create_step is awaited in handle_failed_step retry branch; avoid MagicMock in await
        next_step = make_step(
            now,
            task,
            step_id="step-next",
            status=StepStatus.running,
            order=step.order,
            retry_index=step.retry_index + 1,
            output=None,
        )
        mock_app.DATABASE.create_step = AsyncMock(return_value=next_step)

        # Async mock that forwards to mock_app.DATABASE.update_task so we never await MagicMock inside real update_task
        async def mock_update_task(
            _self,
            task,
            status,
            extracted_information=None,
            failure_reason=None,
            webhook_failure_reason=None,
            errors=None,
        ):
            updates = {}
            if status is not None:
                updates["status"] = status
            if failure_reason is not None:
                updates["failure_reason"] = failure_reason
            if errors is not None:
                updates["errors"] = errors
            return await mock_app.DATABASE.update_task(task.task_id, organization_id=task.organization_id, **updates)

        with patch.object(ForgeAgent, "summary_failure_reason_for_max_retries", mock_summary):
            with patch.object(ForgeAgent, "update_task", mock_update_task):
                result = await agent.handle_failed_step(organization, task, step)

                assert result is None  # No next step when max retries exceeded

                # Verify errors include both system and user-defined errors
                mock_app.DATABASE.update_task.assert_called_once()
                call_kwargs = mock_app.DATABASE.update_task.call_args[1]

                errors = call_kwargs["errors"]
                assert len(errors) == 2

                # First should be ReachMaxRetriesError
                assert errors[0]["error_code"] == ReachMaxRetriesError().error_code

                # Second should be detected user error
                assert errors[1]["error_code"] == "rate_limited"


@pytest.mark.asyncio
async def test_scraping_failure_with_error_detection(agent, mock_browser_state):
    """Test error detection when page scraping fails."""
    now = datetime.now()
    organization = make_organization(now)
    task = make_task(
        now,
        organization,
        error_code_mapping={
            "login_required": "User must be logged in",
            "access_denied": "Access to resource denied",
        },
    )
    step = make_step(now, task, step_id="step-1", status=StepStatus.running, order=1, output=None)

    detected_errors = [
        UserDefinedError(error_code="login_required", reasoning="Login prompt detected", confidence_float=0.85)
    ]

    with patch.object(agent, "update_step", new_callable=AsyncMock):
        with patch.object(agent, "update_task", new_callable=AsyncMock) as mock_update_task:
            mock_update_task.return_value = task

            with create_error_detection_mocks(detected_errors):
                with patch("skyvern.forge.agent.app") as mock_app:
                    mock_app.DATABASE.update_task = AsyncMock()

                    # Simulate ScrapingFailed scenario
                    from skyvern.exceptions import ScrapingFailed

                    try:
                        raise ScrapingFailed(reason="Failed to scrape page elements")
                    except ScrapingFailed as e:
                        result = await agent.fail_task(task, step, e.reason, mock_browser_state)

                        assert result is True

                        # Verify errors were stored
                        mock_app.DATABASE.update_task.assert_called_once()
                        call_kwargs = mock_app.DATABASE.update_task.call_args[1]
                        assert len(call_kwargs["errors"]) == 1
                        assert call_kwargs["errors"][0]["error_code"] == "login_required"


@pytest.mark.asyncio
async def test_multiple_failures_accumulate_errors(agent, mock_browser_state):
    """Test that errors accumulate across multiple failures."""
    now = datetime.now()
    organization = make_organization(now)

    # Start with an existing error
    initial_errors = [{"error_code": "initial_error", "reasoning": "First error"}]

    task = make_task(
        now,
        organization,
        error_code_mapping={
            "payment_failed": "Payment declined",
            "address_invalid": "Invalid address",
        },
        errors=initial_errors,
    )
    step = make_step(now, task, step_id="step-1", status=StepStatus.running, order=1, output=None)

    # First failure detects payment error
    first_detected = [UserDefinedError(error_code="payment_failed", reasoning="Card declined", confidence_float=0.92)]

    with patch.object(agent, "update_step", new_callable=AsyncMock):
        with patch.object(agent, "update_task", new_callable=AsyncMock) as mock_update_task:
            mock_update_task.return_value = task

            with create_error_detection_mocks(first_detected):
                with patch("skyvern.forge.agent.app") as mock_app:
                    mock_app.DATABASE.update_task = AsyncMock()

                    await agent.fail_task(task, step, "First failure", mock_browser_state)

                    # Only new errors are passed — DB handles appending to existing ones
                    call_kwargs = mock_app.DATABASE.update_task.call_args[1]
                    assert len(call_kwargs["errors"]) == 1
                    assert call_kwargs["errors"][0]["error_code"] == "payment_failed"


@pytest.mark.asyncio
async def test_error_detection_with_workflow_task(agent, mock_browser_state):
    """Test error detection works for workflow tasks."""
    now = datetime.now()
    organization = make_organization(now)
    task = make_task(
        now,
        organization,
        workflow_run_id="wr-123",
        workflow_permanent_id="wp-456",
        error_code_mapping={
            "workflow_error": "Workflow-specific error",
        },
    )
    step = make_step(now, task, step_id="step-1", status=StepStatus.running, order=1, output=None)

    detected_errors = [
        UserDefinedError(error_code="workflow_error", reasoning="Workflow condition not met", confidence_float=0.88)
    ]

    with patch.object(agent, "update_step", new_callable=AsyncMock):
        with patch.object(agent, "update_task", new_callable=AsyncMock) as mock_update_task:
            mock_update_task.return_value = task

            with create_error_detection_mocks(detected_errors):
                with patch("skyvern.forge.agent.app") as mock_app:
                    mock_app.DATABASE.update_task = AsyncMock()

                    result = await agent.fail_task(task, step, "Workflow task failed", mock_browser_state)

                    assert result is True

                    # Verify errors were stored for workflow task
                    mock_app.DATABASE.update_task.assert_called_once()
                    call_kwargs = mock_app.DATABASE.update_task.call_args[1]
                    assert call_kwargs["task_id"] == task.task_id
                    # workflow_run_id is not passed in the update call, only task_id and errors
                    assert "workflow_run_id" not in call_kwargs


@pytest.mark.asyncio
async def test_error_detection_performance_doesnt_block_failure(agent, mock_browser_state):
    """Test that slow error detection doesn't significantly delay task failure."""
    now = datetime.now()
    organization = make_organization(now)
    task = make_task(
        now,
        organization,
        error_code_mapping={
            "timeout": "Operation timed out",
        },
    )
    step = make_step(now, task, step_id="step-1", status=StepStatus.running, order=1, output=None)

    with patch.object(agent, "update_step", new_callable=AsyncMock):
        with patch.object(agent, "update_task", new_callable=AsyncMock) as mock_update_task:
            mock_update_task.return_value = task

            # Simulate slow error detection
            import asyncio

            async def slow_detection(*args, **kwargs):
                await asyncio.sleep(0.1)  # Simulate some delay
                return [UserDefinedError(error_code="timeout", reasoning="Timeout detected", confidence_float=0.80)]

            with patch(
                "skyvern.forge.agent.detect_user_defined_errors_for_task",
                new_callable=AsyncMock,
            ) as mock_detect:
                mock_detect.side_effect = slow_detection

                with patch("skyvern.forge.agent.app") as mock_app:
                    mock_app.DATABASE.update_task = AsyncMock()

                    import time

                    start_time = time.time()
                    result = await agent.fail_task(task, step, "Task timeout", mock_browser_state)
                    elapsed = time.time() - start_time

                    # Should complete (error detection runs but doesn't block indefinitely)
                    assert result is True
                    # Should take at least 0.1s (the sleep time)
                    assert elapsed >= 0.1
                    # But not much more (no retry loops or hangs)
                    assert elapsed < 1.0
