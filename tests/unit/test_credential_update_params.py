"""Tests that update_credential() accepts user_context and save_browser_session_intent
on CredentialRepository."""

from unittest.mock import MagicMock, patch

import pytest

from skyvern.forge.sdk.db.repositories.credentials import CredentialRepository
from tests.unit.conftest import MockAsyncSessionCtx, make_mock_session


def _make_credential_repo(mock_credential: MagicMock) -> CredentialRepository:
    mock_session = make_mock_session(mock_credential)
    return CredentialRepository(session_factory=lambda: MockAsyncSessionCtx(mock_session))


# --- CredentialRepository tests ---


@pytest.mark.asyncio
async def test_repo_update_credential_accepts_user_context() -> None:
    mock_credential = MagicMock()
    mock_credential.name = "test"
    mock_credential.user_context = None
    repo = _make_credential_repo(mock_credential)

    with patch("skyvern.forge.sdk.schemas.credentials.Credential.model_validate", return_value=MagicMock()):
        await repo.update_credential(
            credential_id="cred_123",
            organization_id="org_123",
            user_context="Click SSO button first",
        )

    assert mock_credential.user_context == "Click SSO button first"


@pytest.mark.asyncio
async def test_repo_update_credential_accepts_save_browser_session_intent() -> None:
    mock_credential = MagicMock()
    mock_credential.name = "test"
    mock_credential.save_browser_session_intent = False
    repo = _make_credential_repo(mock_credential)

    with patch("skyvern.forge.sdk.schemas.credentials.Credential.model_validate", return_value=MagicMock()):
        await repo.update_credential(
            credential_id="cred_123",
            organization_id="org_123",
            save_browser_session_intent=True,
        )

    assert mock_credential.save_browser_session_intent is True


@pytest.mark.asyncio
async def test_repo_update_credential_unset_params_not_applied() -> None:
    mock_credential = MagicMock()
    mock_credential.name = "test"
    mock_credential.user_context = "existing"
    mock_credential.save_browser_session_intent = True
    repo = _make_credential_repo(mock_credential)

    with patch("skyvern.forge.sdk.schemas.credentials.Credential.model_validate", return_value=MagicMock()):
        await repo.update_credential(
            credential_id="cred_123",
            organization_id="org_123",
        )

    assert mock_credential.user_context == "existing"
    assert mock_credential.save_browser_session_intent is True
