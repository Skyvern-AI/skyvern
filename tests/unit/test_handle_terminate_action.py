"""Tests for handle_terminate_action error handling.

Verifies that when extract_user_defined_errors fails (e.g., CDP disconnection),
the original error codes from the action reasoning are preserved instead of being lost.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.errors.errors import UserDefinedError
from skyvern.webeye.actions.actions import TerminateAction
from skyvern.webeye.actions.handler import handle_terminate_action
from skyvern.webeye.actions.responses import ActionSuccess


def _make_task(error_code_mapping: dict | None = None) -> MagicMock:
    task = MagicMock()
    task.task_id = "tsk_test"
    task.error_code_mapping = error_code_mapping
    return task


def _make_step() -> MagicMock:
    step = MagicMock()
    step.step_id = "stp_test"
    return step


@pytest.mark.asyncio
async def test_terminate_preserves_errors_when_extract_fails() -> None:
    """When extract_user_defined_errors raises, action.errors from LLM reasoning should be preserved."""
    task = _make_task(error_code_mapping={"OTP_TIMEOUT": "OTP verification code not received"})
    step = _make_step()
    page = MagicMock()
    scraped_page = MagicMock()

    original_errors = [UserDefinedError(error_code="OTP_TIMEOUT", reasoning="No TOTP found", confidence_float=0.95)]
    action = TerminateAction(reasoning="No TOTP verification code found", errors=original_errors)

    with patch(
        "skyvern.webeye.actions.handler.extract_user_defined_errors",
        new_callable=AsyncMock,
        side_effect=RuntimeError("Target page, context or browser has been closed"),
    ):
        results = await handle_terminate_action(
            action=action, page=page, scraped_page=scraped_page, task=task, step=step
        )

    assert len(results) == 1
    assert isinstance(results[0], ActionSuccess)
    # The original errors from LLM reasoning must be preserved
    assert len(action.errors) == 1
    assert action.errors[0].error_code == "OTP_TIMEOUT"


@pytest.mark.asyncio
async def test_terminate_uses_extracted_errors_on_success() -> None:
    """When extract_user_defined_errors succeeds, its result should replace action.errors."""
    task = _make_task(error_code_mapping={"OTP_TIMEOUT": "OTP verification code not received"})
    step = _make_step()
    page = MagicMock()
    scraped_page = MagicMock()

    action = TerminateAction(reasoning="No TOTP verification code found")

    extracted_errors = [
        UserDefinedError(error_code="OTP_TIMEOUT", reasoning="Page shows OTP not received", confidence_float=0.99)
    ]
    with patch(
        "skyvern.webeye.actions.handler.extract_user_defined_errors",
        new_callable=AsyncMock,
        return_value=extracted_errors,
    ):
        results = await handle_terminate_action(
            action=action, page=page, scraped_page=scraped_page, task=task, step=step
        )

    assert len(results) == 1
    assert isinstance(results[0], ActionSuccess)
    assert action.errors == extracted_errors


@pytest.mark.asyncio
async def test_terminate_skips_extraction_without_error_code_mapping() -> None:
    """When task has no error_code_mapping, extract_user_defined_errors should not be called."""
    task = _make_task(error_code_mapping=None)
    step = _make_step()
    page = MagicMock()
    scraped_page = MagicMock()

    action = TerminateAction(reasoning="done")

    with patch(
        "skyvern.webeye.actions.handler.extract_user_defined_errors",
        new_callable=AsyncMock,
    ) as mock_extract:
        results = await handle_terminate_action(
            action=action, page=page, scraped_page=scraped_page, task=task, step=step
        )

    mock_extract.assert_not_called()
    assert len(results) == 1
    assert isinstance(results[0], ActionSuccess)
