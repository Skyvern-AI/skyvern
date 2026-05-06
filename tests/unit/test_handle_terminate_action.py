"""Tests for handle_terminate_action error handling.

Verifies that when extract_user_defined_errors fails (e.g., CDP disconnection),
the original error codes from the action reasoning are preserved instead of being lost.
"""

from datetime import timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.errors.errors import UserDefinedError
from skyvern.webeye.actions.actions import TerminateAction
from skyvern.webeye.actions.handler import extract_user_defined_errors, handle_terminate_action
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


async def _extract_with_empty_llm(
    task: MagicMock, step: MagicMock, reasoning: str, monkeypatch: pytest.MonkeyPatch
) -> list[UserDefinedError]:
    scraped_page = MagicMock()
    refreshed_page = MagicMock()
    refreshed_page.build_element_tree.return_value = ""
    refreshed_page.url = "chrome-error://chromewebdata/"
    refreshed_page.screenshots = []
    scraped_page.refresh = AsyncMock(return_value=refreshed_page)

    monkeypatch.setattr(
        "skyvern.webeye.actions.handler.app.EXTRACTION_LLM_API_HANDLER",
        AsyncMock(return_value={"errors": []}),
        raising=False,
    )
    with (
        patch(
            "skyvern.webeye.actions.handler.get_action_history",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "skyvern.webeye.actions.handler.skyvern_context.ensure_context",
            return_value=SimpleNamespace(tz_info=timezone.utc),
        ),
    ):
        return await extract_user_defined_errors(task=task, step=step, scraped_page=scraped_page, reasoning=reasoning)


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
async def test_extract_user_defined_errors_falls_back_to_reasoning_match(monkeypatch: pytest.MonkeyPatch) -> None:
    task = _make_task(error_code_mapping={"portal_inaccessible": "portal is inaccessible"})
    task.navigation_goal = "Download invoices"
    task.navigation_payload = {}
    step = _make_step()

    reasoning = (
        "The page is a browser network error (ERR_CONNECTION_CLOSED) indicating the portal is inaccessible; "
        "terminating avoids further actions that cannot succeed."
    )
    errors = await _extract_with_empty_llm(task=task, step=step, reasoning=reasoning, monkeypatch=monkeypatch)

    assert len(errors) == 1
    assert errors[0].error_code == "portal_inaccessible"
    assert errors[0].reasoning == reasoning


@pytest.mark.asyncio
async def test_extract_user_defined_errors_uses_word_boundary_for_code_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _make_task(
        error_code_mapping={
            "closed": "connection closed",
            "portal_inaccessible": "portal is inaccessible",
        }
    )
    task.navigation_goal = "Download invoices"
    task.navigation_payload = {}
    step = _make_step()

    reasoning = (
        "The page is a browser network error (ERR_CONNECTION_CLOSED) indicating the portal is inaccessible; "
        "terminating avoids further actions that cannot succeed."
    )
    errors = await _extract_with_empty_llm(task=task, step=step, reasoning=reasoning, monkeypatch=monkeypatch)

    assert len(errors) == 1
    assert errors[0].error_code == "portal_inaccessible"


@pytest.mark.asyncio
async def test_extract_user_defined_errors_logs_additional_reasoning_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _make_task(
        error_code_mapping={
            "portal_inaccessible": "portal is inaccessible",
            "network_error": "browser network error",
        }
    )
    task.navigation_goal = "Download invoices"
    task.navigation_payload = {}
    step = _make_step()

    reasoning = (
        "The page is a browser network error (ERR_CONNECTION_CLOSED) indicating the portal is inaccessible; "
        "terminating avoids further actions that cannot succeed."
    )
    with patch("skyvern.webeye.actions.handler.LOG.warning") as mock_warning:
        errors = await _extract_with_empty_llm(task=task, step=step, reasoning=reasoning, monkeypatch=monkeypatch)

    assert len(errors) == 1
    assert errors[0].error_code == "portal_inaccessible"
    mock_warning.assert_called_once_with(
        "Multiple user-defined error mappings matched terminate reasoning; using first match",
        task_id=task.task_id,
        step_id=step.step_id,
        matched_error_codes=["portal_inaccessible", "network_error"],
        selected_error_code="portal_inaccessible",
    )


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
