"""Caller-attribution wiring for ``skyvern.agent.complete_verify``."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from skyvern.errors.errors import CredentialCheckEvidence, LoginUnconfirmedError, UserDefinedError
from skyvern.webeye.actions import actions
from skyvern.webeye.actions.handler import handle_complete_action


@pytest.mark.asyncio
async def test_handle_complete_action_forwards_complete_action_forced_to_complete_verify() -> None:
    action = actions.CompleteAction(verified=False)
    page = MagicMock()
    scraped_page = MagicMock()
    task = MagicMock()
    task.navigation_goal = "Submit the form"
    task.workflow_run_id = "wr_456"
    step = MagicMock()

    captured: dict = {}

    async def fake_complete_verify(*_args, **kwargs):
        captured.update(kwargs)
        result = MagicMock()
        result.is_terminate = False
        result.thoughts = "ok"
        return result

    with patch("skyvern.webeye.actions.handler.app.agent.complete_verify", side_effect=fake_complete_verify):
        await handle_complete_action(action, page, scraped_page, task, step)

    assert captured.get("verification_trigger") == "complete_action_forced"


@pytest.mark.asyncio
async def test_complete_action_gates_model_returned_credential_errors() -> None:
    action = actions.CompleteAction(
        verified=True,
        reasoning="The model concluded the login failed.",
        errors=[UserDefinedError(error_code="invalid_credentials", reasoning="Rejected", confidence_float=1.0)],
    )
    scraped_page = MagicMock()
    scraped_page.url = "https://example.com/login"
    scraped_page.build_element_tree.return_value = "<html>Rejected</html>"
    scraped_page.screenshots = [b"image"]
    task = MagicMock()
    step = MagicMock()
    gated = (
        LoginUnconfirmedError().to_user_defined_error().with_credential_check(CredentialCheckEvidence(confirmed=False))
    )

    with patch("skyvern.webeye.actions.handler.confirm_credential_verdicts", return_value=[gated]) as confirm:
        await handle_complete_action(action, MagicMock(), scraped_page, task, step)

    assert action.errors == [gated]
    assert confirm.await_args.kwargs["errors"][0].error_code == "invalid_credentials"
    assert confirm.await_args.kwargs["context"].current_url == scraped_page.url
