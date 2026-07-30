import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from skyvern.forge import app
from skyvern.forge.sdk.schemas.credentials import (
    CreateCredentialRequest,
    Credential,
    CredentialType,
    CredentialVaultType,
    NonEmptyPasswordCredential,
)
from skyvern.forge.sdk.services.credential import bitwarden_credential_service as service_module
from skyvern.forge.sdk.services.credential.bitwarden_credential_service import BitwardenCredentialVaultService


def _credential() -> Credential:
    return Credential(
        credential_id="cred_test",
        organization_id="org_test",
        name="Login",
        vault_type=CredentialVaultType.BITWARDEN,
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
) -> tuple[BitwardenCredentialVaultService, AsyncMock]:
    service = BitwardenCredentialVaultService()
    monkeypatch.setattr(
        app.DATABASE.credentials,
        "get_organization_bitwarden_collection",
        AsyncMock(return_value=SimpleNamespace(collection_id="collection_test")),
    )
    monkeypatch.setattr(
        service_module.BitwardenService,
        "create_credential_item",
        AsyncMock(return_value="item_new"),
    )
    delete_item = AsyncMock(side_effect=delete_failure)
    monkeypatch.setattr(service_module.BitwardenService, "delete_credential_item", delete_item)
    monkeypatch.setattr(service, "_update_db_credential", AsyncMock(side_effect=failure))
    return service, delete_item


@pytest.mark.asyncio
async def test_post_delete_credential_item_returns_provider_failure_status(monkeypatch: pytest.MonkeyPatch) -> None:
    delete_item = AsyncMock(side_effect=RuntimeError("vault unavailable"))
    monkeypatch.setattr(service_module.BitwardenService, "delete_credential_item", delete_item)

    result = await BitwardenCredentialVaultService().post_delete_credential_item("item_old", "org_1")

    assert result is False
    delete_item.assert_awaited_once_with("item_old")


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
    service, delete_item = _prepare_update(monkeypatch, failure_type(message))

    with pytest.raises(failure_type):
        await service.update_credential(_credential(), _password_request())

    delete_item.assert_awaited_with("item_new")


@pytest.mark.asyncio
async def test_update_credential_enqueues_orphan_when_cancelled_inline_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, delete_item = _prepare_update(
        monkeypatch,
        asyncio.CancelledError(),
        delete_failure=RuntimeError("vault unavailable"),
    )
    orphaned = AsyncMock()
    monkeypatch.setattr(app.AGENT_FUNCTION, "on_credential_item_orphaned", orphaned)

    with pytest.raises(asyncio.CancelledError):
        await service.update_credential(_credential(), _password_request())

    delete_item.assert_awaited_with("item_new")
    orphaned.assert_awaited_with(
        organization_id="org_test",
        item_id="item_new",
        vault_type=CredentialVaultType.BITWARDEN,
    )
