from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.forge import app
from skyvern.forge.sdk.schemas.credentials import PasswordCredential, TotpType
from skyvern.forge.sdk.services.credential import azure_credential_vault_service as service_module
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

    async def read_secret(**_kwargs: object) -> str:
        calls.append("vault_read")
        return "stored-value"

    async def scrub(**_kwargs: object) -> str:
        calls.append("vault_scrub")
        return "secret-id"

    async def db_delete(*_args: object, **_kwargs: object) -> None:
        calls.append("db_delete")

    client = AsyncMock()
    client.get_secret = AsyncMock(side_effect=read_secret)
    client.create_or_update_secret = AsyncMock(side_effect=scrub)
    service = AzureCredentialVaultService(client=client, vault_name="vault")
    monkeypatch.setattr(
        app.DATABASE, "credentials", SimpleNamespace(delete_credential=AsyncMock(side_effect=db_delete))
    )

    await service.delete_credential(SimpleNamespace(item_id="item_1", credential_id="cred_1", organization_id="org_1"))

    # Scrub the still-readable secret before dropping the DB row so a vault failure cannot orphan it.
    assert calls == ["vault_read", "vault_scrub", "db_delete"]
    client.get_secret.assert_awaited_once_with(secret_name="item_1", vault_name="vault")
    client.create_or_update_secret.assert_awaited_once_with(vault_name="vault", secret_name="item_1", secret_value="")


@pytest.mark.asyncio
async def test_azure_delete_leaves_db_row_when_vault_scrub_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    client = AsyncMock()
    client.get_secret = AsyncMock(return_value="stored-value")
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


@pytest.mark.asyncio
async def test_azure_delete_restores_vault_value_when_scrub_partially_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    stored_value = "stored-value"

    async def scrub_or_restore(*, secret_value: str, **_kwargs: object) -> str:
        nonlocal stored_value
        stored_value = secret_value
        if secret_value == "":
            raise RuntimeError("vault connection dropped after scrub")
        return "item_1"

    client = AsyncMock()
    client.get_secret = AsyncMock(return_value=stored_value)
    client.create_or_update_secret = AsyncMock(side_effect=scrub_or_restore)
    service = AzureCredentialVaultService(client=client, vault_name="vault")
    db_delete = AsyncMock()
    monkeypatch.setattr(app.DATABASE, "credentials", SimpleNamespace(delete_credential=db_delete))

    with pytest.raises(RuntimeError, match="vault connection dropped after scrub"):
        await service.delete_credential(
            SimpleNamespace(item_id="item_1", credential_id="cred_1", organization_id="org_1")
        )

    assert stored_value == "stored-value"
    assert [call.kwargs["secret_value"] for call in client.create_or_update_secret.await_args_list] == [
        "",
        "stored-value",
    ]
    db_delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_azure_delete_leaves_db_row_when_vault_read_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    client = AsyncMock()
    client.get_secret = AsyncMock(side_effect=RuntimeError("vault read unavailable"))
    client.create_or_update_secret = AsyncMock()
    service = AzureCredentialVaultService(client=client, vault_name="vault")
    db_delete = AsyncMock()
    monkeypatch.setattr(app.DATABASE, "credentials", SimpleNamespace(delete_credential=db_delete))

    with pytest.raises(RuntimeError, match="vault read unavailable"):
        await service.delete_credential(
            SimpleNamespace(item_id="item_1", credential_id="cred_1", organization_id="org_1")
        )

    client.create_or_update_secret.assert_not_awaited()
    db_delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_azure_delete_removes_db_row_when_vault_item_already_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = AsyncMock()
    client.get_secret = AsyncMock(return_value=None)
    service = AzureCredentialVaultService(client=client, vault_name="vault")
    db_delete = AsyncMock()
    monkeypatch.setattr(app.DATABASE, "credentials", SimpleNamespace(delete_credential=db_delete))

    await service.delete_credential(SimpleNamespace(item_id="item_1", credential_id="cred_1", organization_id="org_1"))

    # A row whose vault item vanished (prior partial delete) must stay deletable: no scrub, straight DB delete.
    client.create_or_update_secret.assert_not_awaited()
    db_delete.assert_awaited_once_with("cred_1", "org_1")


@pytest.mark.asyncio
async def test_azure_delete_restores_vault_value_when_db_delete_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    client = AsyncMock()
    client.get_secret = AsyncMock(return_value="stored-value")
    client.create_or_update_secret = AsyncMock(return_value="item_1")
    service = AzureCredentialVaultService(client=client, vault_name="vault")
    monkeypatch.setattr(
        app.DATABASE,
        "credentials",
        SimpleNamespace(delete_credential=AsyncMock(side_effect=RuntimeError("database unavailable"))),
    )

    with pytest.raises(RuntimeError, match="database unavailable"):
        await service.delete_credential(
            SimpleNamespace(item_id="item_1", credential_id="cred_1", organization_id="org_1")
        )

    assert [call.kwargs["secret_value"] for call in client.create_or_update_secret.await_args_list] == [
        "",
        "stored-value",
    ]


@pytest.mark.asyncio
async def test_azure_delete_reraises_db_error_when_vault_restore_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    client = AsyncMock()
    client.get_secret = AsyncMock(return_value="stored-value")
    client.create_or_update_secret = AsyncMock(side_effect=["item_1", RuntimeError("restore failed")])
    service = AzureCredentialVaultService(client=client, vault_name="vault")
    monkeypatch.setattr(
        app.DATABASE,
        "credentials",
        SimpleNamespace(delete_credential=AsyncMock(side_effect=RuntimeError("database unavailable"))),
    )
    log_error = MagicMock()
    monkeypatch.setattr(service_module.LOG, "error", log_error)

    with pytest.raises(RuntimeError, match="database unavailable"):
        await service.delete_credential(
            SimpleNamespace(item_id="item_1", credential_id="cred_1", organization_id="org_1")
        )

    assert [call.kwargs["secret_value"] for call in client.create_or_update_secret.await_args_list] == [
        "",
        "stored-value",
    ]
    log_error.assert_called_once()
    assert log_error.call_args.kwargs["credential_id"] == "cred_1"
    assert log_error.call_args.kwargs["item_id"] == "item_1"
    assert log_error.call_args.kwargs["exc_info"] is True
