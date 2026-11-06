"""AWS Secrets Manager credential vault contracts, run against a moto Secrets Manager server."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Generator, cast
from unittest.mock import AsyncMock, MagicMock

import boto3
import pytest
from botocore.exceptions import ClientError
from moto.server import ThreadedMotoServer

import skyvern.forge.sdk.services.credential.aws_secrets_manager_credential_vault_service as vault_module
from skyvern.exceptions import CredentialItemNotFoundError
from skyvern.forge.sdk.api.aws import AsyncAWSClient
from skyvern.forge.sdk.schemas.credentials import (
    CreateCredentialRequest,
    Credential,
    CredentialType,
    CredentialVaultType,
    CreditCardCredential,
    NonEmptyCreditCardCredential,
    NonEmptyPasswordCredential,
    PasswordCredential,
    SecretCredential,
    TotpType,
)
from skyvern.forge.sdk.services.credential.aws_secrets_manager_credential_vault_service import (
    AwsSecretsManagerCredentialVaultService,
)

REGION = "us-west-1"
PREFIX = "test-credentials/"
ORG = "o_123"
OTHER_ORG = "o_456"


@pytest.fixture(scope="module")
def moto_server() -> Generator[str]:
    # Loopback, not 0.0.0.0: a macOS system proxy bypasses 127.0.0.1 but would otherwise relay the traffic.
    server = ThreadedMotoServer(ip_address="127.0.0.1", port=0)
    server.start()
    host, port = server.get_host_and_port()
    yield f"http://{host}:{port}"
    server.stop()


@pytest.fixture(autouse=True)
def aws_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")


@pytest.fixture
def secrets(moto_server: str) -> Any:
    return boto3.client(
        "secretsmanager",
        region_name=REGION,
        endpoint_url=moto_server,
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )


@pytest.fixture
def database(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    fake_app = MagicMock()
    fake_app.DATABASE.credentials.delete_credential = AsyncMock()
    monkeypatch.setattr(vault_module, "app", fake_app)
    return fake_app.DATABASE


@pytest.fixture
def vault(
    moto_server: str, monkeypatch: pytest.MonkeyPatch, database: MagicMock
) -> AwsSecretsManagerCredentialVaultService:
    service = AwsSecretsManagerCredentialVaultService(
        AsyncAWSClient(region_name=REGION, endpoint_url=moto_server), name_prefix=PREFIX
    )

    async def create_row(**kwargs: Any) -> Credential:
        assert kwargs["vault_type"] == CredentialVaultType.AWS_SECRETS_MANAGER
        return _row(item_id=kwargs["item_id"], name=kwargs["data"].name)

    monkeypatch.setattr(service, "_create_db_credential", create_row)
    monkeypatch.setattr(
        service,
        "_update_db_credential",
        AsyncMock(side_effect=lambda **kwargs: _row(item_id=kwargs["item_id"], name=kwargs["data"].name)),
    )
    return service


def _row(item_id: str, name: str) -> Credential:
    return cast(
        Credential,
        SimpleNamespace(
            credential_id="cred_1",
            organization_id=ORG,
            item_id=item_id,
            name=name,
            totp_type=TotpType.AUTHENTICATOR,
            has_totp_seed=True,
        ),
    )


def _secret_exists(secrets: Any, name: str) -> bool:
    try:
        secrets.get_secret_value(SecretId=name)
        return True
    except ClientError as e:
        assert e.response["Error"]["Code"] == "ResourceNotFoundException"
        return False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("credential_type", "credential"),
    [
        (
            CredentialType.PASSWORD,
            NonEmptyPasswordCredential(
                username="user@example.com", password="pw", totp="JBSWY3DPEHPK3PXP", metadata={"tenant": "north"}
            ),
        ),
        (
            CredentialType.CREDIT_CARD,
            NonEmptyCreditCardCredential(
                card_number="4242424242424242",
                card_cvv="123",
                card_exp_month="12",
                card_exp_year="2030",
                card_brand="visa",
                card_holder_name="Test Holder",
            ),
        ),
        (CredentialType.SECRET, SecretCredential(secret_value="s3cr3t", secret_label="api key")),
    ],
)
async def test_created_credential_reads_back_from_its_own_secret(
    vault: AwsSecretsManagerCredentialVaultService,
    secrets: Any,
    credential_type: CredentialType,
    credential: PasswordCredential | CreditCardCredential | SecretCredential,
) -> None:
    created = await vault.create_credential(
        ORG, CreateCredentialRequest(name="login", credential_type=credential_type, credential=credential)
    )

    assert created.item_id.startswith(f"{PREFIX}{ORG}/")
    assert _secret_exists(secrets, created.item_id)
    item = await vault.get_credential_item(created)
    assert item.credential_type == credential_type
    assert item.credential.model_dump(exclude={"totp_type"}) == credential.model_dump(exclude={"totp_type"})


@pytest.mark.asyncio
async def test_update_writes_a_new_secret_and_delete_removes_it_without_recovery(
    vault: AwsSecretsManagerCredentialVaultService, secrets: Any, database: MagicMock
) -> None:
    created = await vault.create_credential(
        ORG,
        CreateCredentialRequest(
            name="login",
            credential_type=CredentialType.PASSWORD,
            credential=NonEmptyPasswordCredential(username="user@example.com", password="old", totp="JBSWY3DPEHPK3PXP"),
        ),
    )

    updated_row = await vault.update_credential(
        created,
        CreateCredentialRequest(
            name="login",
            credential_type=CredentialType.PASSWORD,
            credential=NonEmptyPasswordCredential(username="user@example.com", password="new"),
        ),
    )

    # A new item ID is what lets the route detect a concurrent update and delete the replaced secret.
    assert updated_row.item_id != created.item_id
    assert updated_row.item_id.startswith(f"{PREFIX}{ORG}/")
    updated = (await vault.get_credential_item(updated_row)).credential
    assert isinstance(updated, PasswordCredential)
    assert (updated.password, updated.totp) == ("new", "JBSWY3DPEHPK3PXP")
    assert await vault.post_delete_credential_item(created.item_id) is True
    assert not _secret_exists(secrets, created.item_id)

    await vault.delete_credential(updated_row)

    assert not _secret_exists(secrets, updated_row.item_id)
    # A recovery window would keep the name reserved; without one it can be created again at once.
    secrets.create_secret(Name=updated_row.item_id, SecretString="{}")
    secrets.delete_secret(SecretId=updated_row.item_id, ForceDeleteWithoutRecovery=True)
    database.credentials.delete_credential.assert_awaited_with("cred_1", ORG)
    assert await vault.post_delete_credential_item(updated_row.item_id) is True


@pytest.mark.asyncio
async def test_missing_secret_is_reported_as_not_found(vault: AwsSecretsManagerCredentialVaultService) -> None:
    with pytest.raises(CredentialItemNotFoundError):
        await vault.get_credential_item(_row(item_id=f"{PREFIX}{ORG}/missing", name="gone"))


@pytest.mark.asyncio
async def test_failed_row_insert_removes_the_new_secret(
    vault: AwsSecretsManagerCredentialVaultService, secrets: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(vault, "_create_db_credential", AsyncMock(side_effect=RuntimeError("db down")))

    with pytest.raises(RuntimeError, match="db down"):
        await vault.create_credential(
            OTHER_ORG,
            CreateCredentialRequest(
                name="login", credential_type=CredentialType.SECRET, credential=SecretCredential(secret_value="x")
            ),
        )

    names = [s["Name"] for s in secrets.list_secrets()["SecretList"]]
    assert not [name for name in names if name.startswith(f"{PREFIX}{OTHER_ORG}/")]
