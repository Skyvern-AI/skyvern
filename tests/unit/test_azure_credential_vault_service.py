from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from skyvern.forge import app
from skyvern.forge.sdk.schemas.credentials import PasswordCredential, TotpType
from skyvern.forge.sdk.services.credential.azure_credential_vault_service import AzureCredentialVaultService

_PASSWORD_METADATA = {"tenant": "north"}


@pytest.mark.asyncio
async def test_azure_password_data_image_round_trips_metadata_and_loads_legacy() -> None:
    stored_value = ""

    async def store_secret(*, secret_value: str, **_: str) -> str:
        nonlocal stored_value
        stored_value = secret_value
        return "secret-id"

    client = AsyncMock()
    client.create_or_update_secret = AsyncMock(side_effect=store_secret)
    client.get_secret = AsyncMock(side_effect=lambda **_: stored_value)
    service = AzureCredentialVaultService(client=client, vault_name="vault")
    item_id = await service._create_azure_secret_item(
        organization_id="org_test",
        credential=PasswordCredential(username="user@example.com", password="pw", metadata=_PASSWORD_METADATA),
    )
    item = await service.get_credential_item(SimpleNamespace(item_id=item_id, totp_type=TotpType.NONE, name="Login"))

    assert isinstance(item.credential, PasswordCredential)
    assert item.credential.metadata == _PASSWORD_METADATA
    stored_value = '{"type":"password","username":"user","password":"pw"}'
    legacy_item = await service.get_credential_item(
        SimpleNamespace(item_id="secret-id", totp_type=TotpType.NONE, name="Legacy Login")
    )
    assert isinstance(legacy_item.credential, PasswordCredential)
    assert legacy_item.credential.metadata is None


@pytest.mark.asyncio
async def test_azure_delete_scrubs_vault_before_removing_db_row(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def scrub(**_kwargs: object) -> str:
        calls.append("vault_scrub")
        return "secret-id"

    async def db_delete(*_args: object, **_kwargs: object) -> None:
        calls.append("db_delete")

    client = AsyncMock()
    client.create_or_update_secret = AsyncMock(side_effect=scrub)
    service = AzureCredentialVaultService(client=client, vault_name="vault")
    monkeypatch.setattr(
        app.DATABASE, "credentials", SimpleNamespace(delete_credential=AsyncMock(side_effect=db_delete))
    )

    await service.delete_credential(SimpleNamespace(item_id="item_1", credential_id="cred_1", organization_id="org_1"))

    # Scrub the still-readable secret before dropping the DB row so a vault failure cannot orphan it.
    assert calls == ["vault_scrub", "db_delete"]
    client.create_or_update_secret.assert_awaited_once_with(vault_name="vault", secret_name="item_1", secret_value="")


@pytest.mark.asyncio
async def test_azure_delete_leaves_db_row_when_vault_scrub_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    client = AsyncMock()
    client.create_or_update_secret = AsyncMock(side_effect=RuntimeError("vault down"))
    service = AzureCredentialVaultService(client=client, vault_name="vault")
    db_delete = AsyncMock()
    monkeypatch.setattr(app.DATABASE, "credentials", SimpleNamespace(delete_credential=db_delete))

    with pytest.raises(RuntimeError, match="vault down"):
        await service.delete_credential(
            SimpleNamespace(item_id="item_1", credential_id="cred_1", organization_id="org_1")
        )

    # A transient vault failure must abort the delete with the DB row (and its item_id) intact so a retry
    # can still scrub the secret, rather than orphaning a still-readable secret with no DB pointer.
    db_delete.assert_not_awaited()
