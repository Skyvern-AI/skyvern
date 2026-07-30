from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

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
