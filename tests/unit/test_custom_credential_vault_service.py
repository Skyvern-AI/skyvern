import asyncio
from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from skyvern.forge import app
from skyvern.forge.sdk.api.custom_credential_client import CustomCredentialAPIClient
from skyvern.forge.sdk.schemas.credentials import (
    CreateCredentialRequest,
    Credential,
    CredentialType,
    CredentialVaultType,
    NonEmptyPasswordCredential,
)
from skyvern.forge.sdk.services.credential.custom_credential_vault_service import CustomCredentialVaultService


def _credential() -> Credential:
    return Credential(
        credential_id="cred_test",
        organization_id="org_test",
        name="Login",
        vault_type=CredentialVaultType.CUSTOM,
        item_id="item_old",
        credential_type=CredentialType.PASSWORD,
        username="user_test",
        totp_type="none",
        totp_identifier=None,
        card_last4=None,
        card_brand=None,
        secret_label=None,
        browser_profile_id=None,
        tested_url=None,
        user_context=None,
        save_browser_session_intent=False,
        folder_id=None,
        created_at=datetime(2026, 1, 1),
        modified_at=datetime(2026, 1, 1),
        deleted_at=None,
    )


def _password_request() -> CreateCredentialRequest:
    return CreateCredentialRequest(
        name="Login Updated",
        credential_type=CredentialType.PASSWORD,
        credential=NonEmptyPasswordCredential(
            username="user_test",
            password="secret_test",
            metadata={"key": "value"},
        ),
    )


def _prepare_update(
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
    *,
    delete_failure: Exception | None = None,
) -> tuple[CustomCredentialVaultService, AsyncMock]:
    client = AsyncMock(spec=CustomCredentialAPIClient)
    client.create_credential.return_value = "item_new"
    client.delete_credential.side_effect = delete_failure
    service = CustomCredentialVaultService(client=client)
    monkeypatch.setattr(service, "_update_db_credential", AsyncMock(side_effect=failure))
    return service, client.delete_credential


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_type", "message"),
    [
        pytest.param(RuntimeError, "database unavailable", id="exception"),
        pytest.param(asyncio.CancelledError, "repoint cancelled", id="cancelled"),
    ],
)
async def test_update_credential_reclaims_new_item_when_repoint_fails(
    monkeypatch: pytest.MonkeyPatch,
    failure_type: type[BaseException],
    message: str,
) -> None:
    service, delete_credential = _prepare_update(monkeypatch, failure_type(message))

    with pytest.raises(failure_type):
        await service.update_credential(_credential(), _password_request())

    delete_credential.assert_awaited_with("item_new")


@pytest.mark.asyncio
async def test_update_credential_enqueues_orphan_when_cancelled_inline_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, delete_credential = _prepare_update(
        monkeypatch,
        asyncio.CancelledError(),
        delete_failure=RuntimeError("vault unavailable"),
    )
    orphaned = AsyncMock()
    monkeypatch.setattr(app.AGENT_FUNCTION, "on_credential_item_orphaned", orphaned)

    with pytest.raises(asyncio.CancelledError):
        await service.update_credential(_credential(), _password_request())

    delete_credential.assert_awaited_with("item_new")
    orphaned.assert_awaited_with(
        organization_id="org_test",
        item_id="item_new",
        vault_type=CredentialVaultType.CUSTOM,
    )
