"""Regression/feature test for Goal 2: seedable persistent profiles.

The credential-test endpoint (POST /v1/credentials/{id}/test, save_browser_profile=true)
is the only path that saves a reusable browser profile. It ran a GENERIC login whose
hardcoded prompt + terminate criterion bail out the instant a site asks for a phone
number — which is exactly the masked phone-number 2FA step some sites use, so the login
never completes and the profile is never saved.

Fix: let the caller pass a custom ``navigation_goal`` / ``terminate_criterion`` (the same
prompt their working ``login`` block uses). These tests assert that, when provided, the
generated LoginBlock uses them instead of the built-in generic prompt — and that the
defaults are still used otherwise.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import skyvern.forge.sdk.routes.credentials as credentials_route
from skyvern.forge.sdk.routes.credentials import (
    LOGIN_TEST_PROMPT,
    LOGIN_TEST_TERMINATE_CRITERION,
)
from skyvern.forge.sdk.routes.credentials import test_credential as run_test_credential_route
from skyvern.forge.sdk.schemas.credentials import CredentialType
from skyvern.forge.sdk.schemas.credentials import TestCredentialRequest as CredentialTestRequest

CUSTOM_GOAL = (
    "Log in with the provided credentials. When the masked phone-number dropdown appears, "
    "select a number with the keyboard (Down then Enter), then enter the TOTP code."
)
CUSTOM_CRITERION = "Only terminate if the credentials are explicitly rejected."


async def _run_test_credential(request: CredentialTestRequest):
    """Drive the route with all external dependencies mocked; return the
    WorkflowCreateYAMLRequest captured at create_workflow_from_request."""
    captured = {}

    credential = MagicMock()
    credential.credential_type = CredentialType.PASSWORD
    credential.browser_profile_id = None
    credential.totp_identifier = "totp-id"
    credential.name = "Test Credential"

    mock_db = MagicMock()
    mock_db.credentials.get_credential = AsyncMock(return_value=credential)

    workflow = MagicMock()
    workflow.workflow_permanent_id = "wpid_1"

    workflow_run = MagicMock()
    workflow_run.workflow_run_id = "wr_1"
    workflow_run.workflow_id = "w_1"
    workflow_run.workflow_permanent_id = "wpid_1"

    async def _capture_create(organization, request):  # noqa: ANN001
        captured["create_request"] = request
        return workflow

    mock_wf_service = MagicMock()
    mock_wf_service.create_workflow_from_request = AsyncMock(side_effect=_capture_create)
    mock_wf_service.setup_workflow_run = AsyncMock(return_value=workflow_run)

    mock_executor = MagicMock()
    mock_executor.execute_workflow = AsyncMock()

    org = MagicMock()
    org.organization_id = "o_1"

    with (
        patch.object(credentials_route, "app") as mock_app,
        patch.object(credentials_route, "AsyncExecutorFactory") as mock_factory,
    ):
        mock_app.DATABASE = mock_db
        mock_app.WORKFLOW_SERVICE = mock_wf_service
        mock_factory.get_executor.return_value = mock_executor

        # save_browser_profile=False so the route does not spawn the polling task.
        await run_test_credential_route(
            background_tasks=MagicMock(),
            credential_id="cred_1",
            data=request,
            current_org=org,
        )

    return captured["create_request"]


@pytest.mark.asyncio
async def test_custom_navigation_goal_and_criterion_used_when_provided() -> None:
    req = CredentialTestRequest(
        url="https://account.example.com/sign-in",
        save_browser_profile=False,
        navigation_goal=CUSTOM_GOAL,
        terminate_criterion=CUSTOM_CRITERION,
    )
    create_request = await _run_test_credential(req)
    block = create_request.workflow_definition.blocks[0]

    assert CUSTOM_GOAL in block.navigation_goal, "custom navigation_goal must drive the login block"
    assert "TERMINATE IMMEDIATELY" not in block.navigation_goal, "built-in phone-number bail-out must be gone"
    assert block.terminate_criterion == CUSTOM_CRITERION


@pytest.mark.asyncio
async def test_defaults_used_when_not_provided() -> None:
    req = CredentialTestRequest(url="https://account.example.com/sign-in", save_browser_profile=False)
    create_request = await _run_test_credential(req)
    block = create_request.workflow_definition.blocks[0]

    assert block.navigation_goal == LOGIN_TEST_PROMPT
    assert block.terminate_criterion == LOGIN_TEST_TERMINATE_CRITERION


@pytest.mark.asyncio
async def test_user_context_still_appended_to_custom_goal() -> None:
    req = CredentialTestRequest(
        url="https://account.example.com/sign-in",
        save_browser_profile=False,
        navigation_goal=CUSTOM_GOAL,
        user_context="The 'Sign in' button is in the top-right corner.",
    )
    create_request = await _run_test_credential(req)
    block = create_request.workflow_definition.blocks[0]

    assert CUSTOM_GOAL in block.navigation_goal
    assert "top-right corner" in block.navigation_goal
