"""Tests for which error codes a terminate action keeps when the task has an error_code_mapping."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.errors.errors import TimeoutGetTOTPVerificationCodeError, UserDefinedError
from skyvern.webeye.actions.actions import TerminateAction
from skyvern.webeye.actions.handler import extract_user_defined_errors, handle_terminate_action
from skyvern.webeye.actions.models import DetailedAgentStepOutput
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


def _make_scraped_page() -> MagicMock:
    scraped_page = MagicMock()
    refreshed_page = MagicMock()
    refreshed_page.build_element_tree.return_value = ""
    refreshed_page.url = "chrome-error://chromewebdata/"
    refreshed_page.screenshots = []
    scraped_page.refresh = AsyncMock(return_value=refreshed_page)
    return scraped_page


@contextmanager
def _surface_errors_llm_returns(monkeypatch: pytest.MonkeyPatch, errors: list[dict[str, Any]]) -> Iterator[None]:
    monkeypatch.setattr(
        "skyvern.webeye.actions.handler.app.EXTRACTION_LLM_API_HANDLER",
        AsyncMock(return_value={"errors": errors}),
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
        yield


async def _extract_with_empty_llm(
    task: MagicMock, step: MagicMock, reasoning: str, monkeypatch: pytest.MonkeyPatch
) -> list[UserDefinedError]:
    with _surface_errors_llm_returns(monkeypatch, []):
        return await extract_user_defined_errors(
            task=task, step=step, scraped_page=_make_scraped_page(), reasoning=reasoning
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_code_mapping", "llm_errors", "expected_codes"),
    [
        pytest.param(
            {"LOGIN_FAILED": "the login did not complete"},
            [],
            ["OTP_TIMEOUT"],
            id="mapping_without_otp_timeout_llm_returns_nothing",
        ),
        pytest.param(
            {"OTP_TIMEOUT": "the 2FA code never arrived", "LOGIN_FAILED": "the login did not complete"},
            [{"error_code": "LOGIN_FAILED", "reasoning": "Still on the login page", "confidence_float": 0.8}],
            ["OTP_TIMEOUT", "LOGIN_FAILED"],
            id="mapping_declares_otp_timeout_llm_omits_it",
        ),
        pytest.param(
            {"OTP_TIMEOUT": "the 2FA code never arrived", "LOGIN_FAILED": "the login did not complete"},
            [
                {"error_code": "OTP_TIMEOUT", "reasoning": "No code arrived", "confidence_float": 0.9},
                {"error_code": "LOGIN_FAILED", "reasoning": "Still on the login page", "confidence_float": 0.8},
            ],
            ["OTP_TIMEOUT", "LOGIN_FAILED"],
            id="mapping_declares_otp_timeout_llm_repeats_it",
        ),
    ],
)
async def test_totp_timeout_code_reaches_step_errors_with_error_code_mapping(
    monkeypatch: pytest.MonkeyPatch,
    error_code_mapping: dict[str, str],
    llm_errors: list[dict[str, Any]],
    expected_codes: list[str],
) -> None:
    task = _make_task(error_code_mapping=error_code_mapping)
    task.navigation_goal = "Log in"
    task.navigation_payload = {}
    action = TerminateAction(
        reasoning="No TOTP verification code found. Going to terminate. Polled source: totp_verification_url=x.",
        errors=[TimeoutGetTOTPVerificationCodeError().to_user_defined_error()],
    )

    with _surface_errors_llm_returns(monkeypatch, llm_errors):
        results = await handle_terminate_action(
            action=action, page=MagicMock(), scraped_page=_make_scraped_page(), task=task, step=_make_step()
        )

    step_output = DetailedAgentStepOutput(
        scraped_page=None,
        extract_action_prompt=None,
        llm_response=None,
        actions=[action],
        action_results=results,
        actions_and_results=[(action, results)],
    )
    assert [error.error_code for error in step_output.extract_errors()] == expected_codes


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "llm_error_code",
    [
        pytest.param("LLM_REASONING_ERROR", id="code_not_in_mapping"),
        pytest.param("OTP_TIMEOUT", id="code_named_like_a_skyvern_code"),
    ],
)
async def test_terminate_keeps_codes_the_llm_put_on_the_action_only_if_extraction_agrees(
    monkeypatch: pytest.MonkeyPatch, llm_error_code: str
) -> None:
    task = _make_task(
        error_code_mapping={"OTP_TIMEOUT": "the 2FA code never arrived", "LOGIN_FAILED": "the login did not complete"}
    )
    task.navigation_goal = "Log in"
    task.navigation_payload = {}
    action = TerminateAction(
        reasoning="The page did not load",
        errors=[UserDefinedError(error_code=llm_error_code, reasoning="Page is blank", confidence_float=0.8)],
    )

    with _surface_errors_llm_returns(monkeypatch, []):
        await handle_terminate_action(
            action=action, page=MagicMock(), scraped_page=_make_scraped_page(), task=task, step=_make_step()
        )

    assert action.errors == []


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
