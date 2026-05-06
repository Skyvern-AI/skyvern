from __future__ import annotations

from types import SimpleNamespace
from typing import Literal
from unittest.mock import AsyncMock

import pytest

from skyvern.client import SkyvernEnvironment, WorkflowRunResponse
from skyvern.library.skyvern_browser_page_agent import SkyvernBrowserPageAgent
from skyvern.schemas.credential_type import CredentialType


def test_login_overload_literal_values_match_credential_type_values() -> None:
    assert CredentialType.skyvern.value == "skyvern"
    assert CredentialType.bitwarden.value == "bitwarden"
    assert CredentialType.onepassword.value == "1password"
    assert CredentialType.azure_vault.value == "azure_vault"


def _workflow_run_response() -> WorkflowRunResponse:
    return WorkflowRunResponse.model_validate(
        {
            "run_id": "wr_123",
            "run_type": "workflow_run",
            "status": "completed",
            "created_at": "2026-05-05T00:00:00Z",
            "modified_at": "2026-05-05T00:00:00Z",
        }
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("credential_type", [CredentialType.skyvern, "skyvern"])
async def test_browser_page_agent_login_normalizes_credential_type(
    credential_type: CredentialType | Literal["skyvern"],
) -> None:
    workflow_run_response = _workflow_run_response()
    skyvern = SimpleNamespace(
        environment=SkyvernEnvironment.LOCAL,
        login=AsyncMock(return_value=workflow_run_response),
    )
    browser = SimpleNamespace(
        skyvern=skyvern,
        browser_session_id="pbs_123",
        browser_address="ws://127.0.0.1:9222",
    )
    agent = SkyvernBrowserPageAgent(browser, page=object())  # type: ignore[arg-type]
    agent._wait_for_run_completion = AsyncMock(return_value=workflow_run_response)  # type: ignore[method-assign]

    response = await agent.login(
        credential_type=credential_type,
        credential_id="cred_123",
        url="https://example.com/login",
    )

    assert response.run_id == "wr_123"
    assert skyvern.login.await_args.kwargs["credential_type"] is CredentialType.skyvern
