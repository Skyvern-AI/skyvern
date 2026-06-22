import json
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from google.api_core.exceptions import AlreadyExists, Conflict, NotFound
from google.cloud import secretmanager_v1

from skyvern.config import settings
from skyvern.forge.sdk.api.real_gcp import RealAsyncGcpSecretManagerClient
from skyvern.forge.sdk.schemas.credentials import (
    CreateCredentialRequest,
    Credential,
    CredentialType,
    CredentialVaultType,
    CreditCardBillingAddress,
    CreditCardCredential,
    PasswordCredential,
    SecretCredential,
    TotpType,
)
from skyvern.forge.sdk.services.credential.gcp_credential_vault_service import GcpCredentialVaultService

TEST_PROJECT = "test-project"
TEST_ORG = "o_123"


def _service() -> tuple[GcpCredentialVaultService, AsyncMock]:
    """Return a service plus the mock vault client it delegates to.

    The client is an AsyncMock (dynamic attributes) so its methods can be
    stubbed without tripping mypy's method-assign check on the Protocol type.
    """
    client = AsyncMock()
    return GcpCredentialVaultService(client=client, project_id=TEST_PROJECT), client


def _db_credential(**fields: object) -> Credential:
    # Only a handful of attributes are read by the service; a SimpleNamespace
    # stands in for a full Credential row.
    return cast(Credential, SimpleNamespace(**fields))


@pytest.mark.asyncio
class TestGcpSecretItemCreation:
    async def test_create_password_secret_item(self) -> None:
        svc, client = _service()
        client.create_or_update_secret = AsyncMock(return_value="ret-id")

        item_id = await svc._create_gcp_secret_item(
            organization_id=TEST_ORG,
            credential=PasswordCredential(username="user@example.com", password="pw", totp="JBSWY3DPEHPK3PXP"),
        )

        assert item_id == "ret-id"
        call = client.create_or_update_secret.call_args.kwargs
        assert call["project_id"] == TEST_PROJECT
        assert call["secret_id"].startswith(f"{settings.GCP_CREDENTIAL_VAULT_PREFIX}{TEST_ORG}-")
        assert json.loads(call["value"]) == {
            "type": "password",
            "password": "pw",
            "username": "user@example.com",
            "totp": "JBSWY3DPEHPK3PXP",
        }

    async def test_create_credit_card_secret_item(self) -> None:
        svc, client = _service()
        client.create_or_update_secret = AsyncMock(return_value="ret-id")

        await svc._create_gcp_secret_item(
            organization_id=TEST_ORG,
            credential=CreditCardCredential(
                card_number="4111111111111111",
                card_cvv="123",
                card_exp_month="12",
                card_exp_year="2030",
                card_brand="visa",
                card_holder_name="John Doe",
                billing_address=CreditCardBillingAddress(
                    line1="123 Main St",
                    city="San Francisco",
                    state_code="CA",
                    postal_code="94105",
                    country_code="US",
                ),
                billing_email="billing@example.com",
            ),
        )

        payload = json.loads(client.create_or_update_secret.call_args.kwargs["value"])
        assert payload["type"] == "credit_card"
        assert payload["card_number"] == "4111111111111111"
        assert payload["card_holder_name"] == "John Doe"
        assert payload["billing_address"]["line1"] == "123 Main St"
        assert payload["billing_email"] == "billing@example.com"

    async def test_create_secret_item_excludes_none_label(self) -> None:
        svc, client = _service()
        client.create_or_update_secret = AsyncMock(return_value="ret-id")

        await svc._create_gcp_secret_item(
            organization_id=TEST_ORG,
            credential=SecretCredential(secret_value="sk-abc123"),
        )

        payload = json.loads(client.create_or_update_secret.call_args.kwargs["value"])
        assert payload == {"type": "secret", "secret_value": "sk-abc123"}  # secret_label dropped by exclude_none

    async def test_create_rejects_unsafe_organization_id(self) -> None:
        svc, client = _service()
        client.create_or_update_secret = AsyncMock(return_value="ret-id")

        with pytest.raises(ValueError, match="not valid for Secret Manager"):
            await svc._create_gcp_secret_item(
                organization_id="org with spaces/and+slashes",
                credential=SecretCredential(secret_value="sk-abc123"),
            )

        client.create_or_update_secret.assert_not_called()

    async def test_update_reuses_item_id(self) -> None:
        svc, client = _service()
        client.create_or_update_secret = AsyncMock(return_value="sid")

        await svc._update_gcp_secret_item(
            item_id="existing-sid",
            credential=PasswordCredential(username="u", password="p"),
        )

        assert client.create_or_update_secret.call_args.kwargs["secret_id"] == "existing-sid"


@pytest.mark.asyncio
class TestGetCredentialItem:
    async def test_get_password_credential(self) -> None:
        svc, client = _service()
        client.get_secret = AsyncMock(
            return_value=json.dumps({"type": "password", "username": "u", "password": "p", "totp": "T"})
        )
        db_cred = _db_credential(item_id="sid", totp_type=TotpType.NONE, name="My Login")

        item = await svc.get_credential_item(db_cred)

        assert item.credential_type == CredentialType.PASSWORD
        assert item.item_id == "sid"
        assert item.name == "My Login"
        assert isinstance(item.credential, PasswordCredential)
        assert item.credential.username == "u"
        assert item.credential.password == "p"

    async def test_get_credit_card_credential(self) -> None:
        svc, client = _service()
        client.get_secret = AsyncMock(
            return_value=json.dumps(
                {
                    "type": "credit_card",
                    "card_number": "4111111111111111",
                    "card_cvv": "123",
                    "card_exp_month": "12",
                    "card_exp_year": "2030",
                    "card_brand": "visa",
                    "card_holder_name": "John Doe",
                    "billing_address": {
                        "line1": "123 Main St",
                        "city": "San Francisco",
                        "state_code": "CA",
                        "postal_code": "94105",
                        "country_code": "US",
                    },
                    "billing_phone": "+14155550123",
                }
            )
        )
        db_cred = _db_credential(item_id="sid", totp_type=TotpType.NONE, name="My Card")

        item = await svc.get_credential_item(db_cred)

        assert item.credential_type == CredentialType.CREDIT_CARD
        assert isinstance(item.credential, CreditCardCredential)
        assert item.credential.card_number == "4111111111111111"
        assert item.credential.billing_address
        assert item.credential.billing_address.country_code == "US"
        assert item.credential.billing_phone == "+14155550123"

    async def test_get_secret_credential(self) -> None:
        svc, client = _service()
        client.get_secret = AsyncMock(
            return_value=json.dumps({"type": "secret", "secret_value": "sk-abc123", "secret_label": "API key"})
        )
        db_cred = _db_credential(item_id="sid", totp_type=TotpType.NONE, name="My Secret")

        item = await svc.get_credential_item(db_cred)

        assert item.credential_type == CredentialType.SECRET
        assert isinstance(item.credential, SecretCredential)
        assert item.credential.secret_value == "sk-abc123"
        assert item.credential.secret_label == "API key"

    async def test_get_nonexistent_credential_raises(self) -> None:
        svc, client = _service()
        client.get_secret = AsyncMock(return_value=None)
        db_cred = _db_credential(item_id="missing", totp_type=TotpType.NONE, name="x")

        with pytest.raises(ValueError, match="GCP Credential Vault secret not found"):
            await svc.get_credential_item(db_cred)


@pytest.mark.asyncio
class TestCreateAndDeleteCredential:
    async def test_create_credential_uses_gcp_vault_type(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import skyvern.forge.sdk.services.credential.credential_vault_service as base_mod

        fake_app = MagicMock()
        fake_app.DATABASE.credentials.create_credential = AsyncMock(return_value="DB_CRED")
        monkeypatch.setattr(base_mod, "app", fake_app)

        svc, client = _service()
        client.create_or_update_secret = AsyncMock(return_value="sid-xyz")
        req = CreateCredentialRequest(
            name="My Login",
            credential_type=CredentialType.PASSWORD,
            credential={"username": "u", "password": "p"},
        )

        result = await svc.create_credential(TEST_ORG, req)

        assert result == "DB_CRED"
        client.create_or_update_secret.assert_awaited_once()
        kwargs = fake_app.DATABASE.credentials.create_credential.call_args.kwargs
        assert kwargs["vault_type"] == CredentialVaultType.GCP
        assert kwargs["item_id"] == "sid-xyz"

    async def test_delete_credential_deletes_db_and_secret(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import skyvern.forge.sdk.services.credential.gcp_credential_vault_service as gcp_mod

        fake_app = MagicMock()
        fake_app.DATABASE.credentials.delete_credential = AsyncMock()
        monkeypatch.setattr(gcp_mod, "app", fake_app)

        order: list[str] = []
        fake_app.DATABASE.credentials.delete_credential = AsyncMock(side_effect=lambda *a: order.append("db"))
        svc, client = _service()
        client.delete_secret = AsyncMock(side_effect=lambda **k: order.append("secret"))
        cred = _db_credential(credential_id="c1", organization_id="o_1", item_id="sid")

        await svc.delete_credential(cred)

        fake_app.DATABASE.credentials.delete_credential.assert_awaited_once_with("c1", "o_1")
        client.delete_secret.assert_awaited_once_with(secret_id="sid", project_id=TEST_PROJECT)
        # Secret must be deleted BEFORE the DB row so a vault error can't orphan it.
        assert order == ["secret", "db"]


@pytest.mark.asyncio
class TestRealSecretManagerClient:
    async def test_create_or_update_creates_then_adds_version(self) -> None:
        mock_client = MagicMock()
        mock_client.list_secret_versions.return_value = []
        c = RealAsyncGcpSecretManagerClient(client=mock_client)

        ret = await c.create_or_update_secret(secret_id="sid", project_id="proj", value="hello")

        assert ret == "sid"
        create_req = mock_client.create_secret.call_args.kwargs["request"]
        assert create_req["parent"] == "projects/proj"
        assert create_req["secret_id"] == "sid"
        add_req = mock_client.add_secret_version.call_args.kwargs["request"]
        assert add_req["parent"] == "projects/proj/secrets/sid"
        assert add_req["payload"]["data"] == b"hello"

    @pytest.mark.parametrize("existing_exc", [Conflict("exists"), AlreadyExists("exists")])
    async def test_create_or_update_ignores_existing_secret(self, existing_exc: Exception) -> None:
        # REST transport raises generic Conflict (409); gRPC raises AlreadyExists.
        # Both mean "secret already exists" and must fall through to add a version.
        mock_client = MagicMock()
        mock_client.create_secret.side_effect = existing_exc
        mock_client.list_secret_versions.return_value = []
        c = RealAsyncGcpSecretManagerClient(client=mock_client)

        ret = await c.create_or_update_secret(secret_id="sid", project_id="proj", value="v")

        assert ret == "sid"
        mock_client.add_secret_version.assert_called_once()  # still adds a new version

    async def test_create_or_update_revokes_prior_versions(self) -> None:
        # After adding the new version, every other non-destroyed version must be
        # destroyed so a rotated/leaked credential can't be read back by version id.
        mock_client = MagicMock()
        added = MagicMock()
        added.name = "projects/proj/secrets/sid/versions/2"
        mock_client.add_secret_version.return_value = added

        prior_enabled = MagicMock()
        prior_enabled.name = "projects/proj/secrets/sid/versions/1"
        prior_enabled.state = secretmanager_v1.SecretVersion.State.ENABLED
        already_destroyed = MagicMock()
        already_destroyed.name = "projects/proj/secrets/sid/versions/0"
        already_destroyed.state = secretmanager_v1.SecretVersion.State.DESTROYED
        mock_client.list_secret_versions.return_value = [prior_enabled, already_destroyed, added]

        c = RealAsyncGcpSecretManagerClient(client=mock_client)
        await c.create_or_update_secret(secret_id="sid", project_id="proj", value="v2")

        # Only the prior ENABLED version is destroyed; the just-added and the
        # already-destroyed versions are skipped.
        destroyed_names = [call.kwargs["request"]["name"] for call in mock_client.destroy_secret_version.call_args_list]
        assert destroyed_names == ["projects/proj/secrets/sid/versions/1"]

    async def test_create_or_update_keeps_concurrently_added_newer_versions(self) -> None:
        # If a concurrent update added a newer version between our add and our
        # list, destroying it would race both writers into destroying each
        # other's version, leaving every version destroyed. Only versions
        # numerically older than ours may be destroyed (last-writer-wins).
        mock_client = MagicMock()
        added = MagicMock()
        added.name = "projects/proj/secrets/sid/versions/2"
        mock_client.add_secret_version.return_value = added

        prior_enabled = MagicMock()
        prior_enabled.name = "projects/proj/secrets/sid/versions/1"
        prior_enabled.state = secretmanager_v1.SecretVersion.State.ENABLED
        concurrent_newer = MagicMock()
        concurrent_newer.name = "projects/proj/secrets/sid/versions/3"
        concurrent_newer.state = secretmanager_v1.SecretVersion.State.ENABLED
        mock_client.list_secret_versions.return_value = [prior_enabled, added, concurrent_newer]

        c = RealAsyncGcpSecretManagerClient(client=mock_client)
        await c.create_or_update_secret(secret_id="sid", project_id="proj", value="v2")

        destroyed_names = [call.kwargs["request"]["name"] for call in mock_client.destroy_secret_version.call_args_list]
        assert destroyed_names == ["projects/proj/secrets/sid/versions/1"]

    async def test_get_secret_returns_decoded(self) -> None:
        mock_client = MagicMock()
        resp = MagicMock()
        resp.payload.data = b"secret-bytes"
        mock_client.access_secret_version.return_value = resp
        c = RealAsyncGcpSecretManagerClient(client=mock_client)

        assert await c.get_secret("sid", "proj") == "secret-bytes"
        name = mock_client.access_secret_version.call_args.kwargs["request"]["name"]
        assert name == "projects/proj/secrets/sid/versions/latest"

    async def test_get_secret_not_found_returns_none(self) -> None:
        mock_client = MagicMock()
        mock_client.access_secret_version.side_effect = NotFound("nope")
        c = RealAsyncGcpSecretManagerClient(client=mock_client)

        assert await c.get_secret("sid", "proj") is None

    async def test_delete_secret(self) -> None:
        mock_client = MagicMock()
        c = RealAsyncGcpSecretManagerClient(client=mock_client)

        await c.delete_secret("sid", "proj")

        assert mock_client.delete_secret.call_args.kwargs["request"]["name"] == "projects/proj/secrets/sid"

    async def test_delete_secret_not_found_swallowed(self) -> None:
        mock_client = MagicMock()
        mock_client.delete_secret.side_effect = NotFound("nope")
        c = RealAsyncGcpSecretManagerClient(client=mock_client)

        await c.delete_secret("sid", "proj")  # must not raise
