from datetime import datetime

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.schemas.credentials import (
    CreateCredentialRequest,
    Credential,
    CredentialType,
    CredentialVaultType,
    NonEmptyPasswordCredential,
)
from skyvern.forge.sdk.services.credential.skyvern_credential_vault_service import (
    SkyvernCredentialVaultService,
)


class _FakeCredentialRepository:
    def __init__(self) -> None:
        self.deleted: tuple[str, str] | None = None

    async def create_credential(self, **kwargs: object) -> Credential:
        return self._credential(
            credential_id="cred_test",
            organization_id=str(kwargs["organization_id"]),
            name=str(kwargs["name"]),
            vault_type=kwargs["vault_type"],
            item_id=str(kwargs["item_id"]),
            credential_type=kwargs["credential_type"],
            username=kwargs["username"],
        )

    async def update_credential_vault_data(self, **kwargs: object) -> Credential:
        return self._credential(
            credential_id=str(kwargs["credential_id"]),
            organization_id=str(kwargs["organization_id"]),
            name=str(kwargs["name"]),
            vault_type=CredentialVaultType.SKYVERN,
            item_id=str(kwargs["item_id"]),
            credential_type=kwargs["credential_type"],
            username=kwargs["username"],
        )

    async def delete_credential(self, credential_id: str, organization_id: str) -> None:
        self.deleted = (credential_id, organization_id)

    @staticmethod
    def _credential(
        *,
        credential_id: str,
        organization_id: str,
        name: str,
        vault_type: object,
        item_id: str,
        credential_type: object,
        username: object,
    ) -> Credential:
        return Credential(
            credential_id=credential_id,
            organization_id=organization_id,
            name=name,
            vault_type=vault_type,
            item_id=item_id,
            credential_type=credential_type,
            username=username,
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


class _FailingCreateCredentialRepository(_FakeCredentialRepository):
    async def create_credential(self, **kwargs: object) -> Credential:
        raise RuntimeError("database unavailable")


class _FailingUpdateCredentialRepository(_FakeCredentialRepository):
    async def update_credential_vault_data(self, **kwargs: object) -> Credential:
        raise RuntimeError("database unavailable")


def _password_request(name: str = "Login", password: str = "secret-password") -> CreateCredentialRequest:
    return CreateCredentialRequest(
        name=name,
        credential_type=CredentialType.PASSWORD,
        credential=NonEmptyPasswordCredential(username="user@example.com", password=password),
    )


@pytest.mark.asyncio
async def test_skyvern_credential_vault_round_trips_encrypted_password(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "LOCAL_CREDENTIAL_VAULT_PATH", str(tmp_path))
    monkeypatch.setattr(settings, "LOCAL_CREDENTIAL_VAULT_KEY", None)

    repository = _FakeCredentialRepository()
    app.DATABASE.credentials = repository

    service = SkyvernCredentialVaultService()
    request = _password_request()

    credential = await service.create_credential("org_test", request)

    assert credential.vault_type == CredentialVaultType.SKYVERN
    item_path = tmp_path / f"{credential.item_id}.bin"
    assert item_path.exists()
    assert b"secret-password" not in item_path.read_bytes()

    item = await service.get_credential_item(credential)
    assert item.name == "Login"
    assert item.credential_type == CredentialType.PASSWORD
    assert item.credential.username == "user@example.com"
    assert item.credential.password == "secret-password"

    await service.delete_credential(credential)
    assert repository.deleted == ("cred_test", "org_test")
    assert not item_path.exists()


@pytest.mark.asyncio
async def test_skyvern_credential_vault_update_creates_new_item_and_cleans_old(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "LOCAL_CREDENTIAL_VAULT_PATH", str(tmp_path))
    monkeypatch.setattr(settings, "LOCAL_CREDENTIAL_VAULT_KEY", None)

    app.DATABASE.credentials = _FakeCredentialRepository()

    service = SkyvernCredentialVaultService()
    request = _password_request(password="old-password")
    credential = await service.create_credential("org_test", request)
    old_item_id = credential.item_id

    update_request = _password_request(name="Login Updated", password="new-password")
    updated = await service.update_credential(credential, update_request)

    assert updated.item_id != old_item_id
    assert (tmp_path / f"{old_item_id}.bin").exists()

    await service.post_delete_credential_item(old_item_id)
    assert not (tmp_path / f"{old_item_id}.bin").exists()

    item = await service.get_credential_item(updated)
    assert item.name == "Login Updated"
    assert item.credential.password == "new-password"


@pytest.mark.asyncio
async def test_skyvern_credential_vault_cleans_item_when_create_db_fails(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "LOCAL_CREDENTIAL_VAULT_PATH", str(tmp_path))
    monkeypatch.setattr(settings, "LOCAL_CREDENTIAL_VAULT_KEY", None)
    app.DATABASE.credentials = _FailingCreateCredentialRepository()

    service = SkyvernCredentialVaultService()

    with pytest.raises(RuntimeError, match="database unavailable"):
        await service.create_credential("org_test", _password_request())

    assert list(tmp_path.glob("*.bin")) == []


@pytest.mark.asyncio
async def test_skyvern_credential_vault_cleans_new_item_when_update_db_fails(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "LOCAL_CREDENTIAL_VAULT_PATH", str(tmp_path))
    monkeypatch.setattr(settings, "LOCAL_CREDENTIAL_VAULT_KEY", None)
    app.DATABASE.credentials = _FailingUpdateCredentialRepository()

    service = SkyvernCredentialVaultService()
    credential = await service.create_credential("org_test", _password_request(password="old-password"))
    old_item_path = tmp_path / f"{credential.item_id}.bin"

    with pytest.raises(RuntimeError, match="database unavailable"):
        await service.update_credential(
            credential,
            _password_request(name="Login Updated", password="new-password"),
        )

    assert old_item_path.exists()
    assert list(tmp_path.glob("*.bin")) == [old_item_path]


@pytest.mark.asyncio
async def test_skyvern_credential_vault_rejects_invalid_item_id(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "LOCAL_CREDENTIAL_VAULT_PATH", str(tmp_path))
    monkeypatch.setattr(settings, "LOCAL_CREDENTIAL_VAULT_KEY", None)
    credential = _FakeCredentialRepository._credential(
        credential_id="cred_test",
        organization_id="org_test",
        name="Login",
        vault_type=CredentialVaultType.SKYVERN,
        item_id="../outside",
        credential_type=CredentialType.PASSWORD,
        username="user@example.com",
    )

    with pytest.raises(HTTPException) as exc_info:
        await SkyvernCredentialVaultService().get_credential_item(credential)

    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_skyvern_credential_vault_wrong_key_cannot_decrypt_item(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "LOCAL_CREDENTIAL_VAULT_PATH", str(tmp_path))
    monkeypatch.setattr(settings, "LOCAL_CREDENTIAL_VAULT_KEY", Fernet.generate_key().decode("utf-8"))
    app.DATABASE.credentials = _FakeCredentialRepository()

    credential = await SkyvernCredentialVaultService().create_credential("org_test", _password_request())
    monkeypatch.setattr(settings, "LOCAL_CREDENTIAL_VAULT_KEY", Fernet.generate_key().decode("utf-8"))

    with pytest.raises(HTTPException) as exc_info:
        await SkyvernCredentialVaultService().get_credential_item(credential)

    assert exc_info.value.status_code == 500
