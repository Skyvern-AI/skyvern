from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from onepassword import ItemFieldType

from skyvern.exceptions import CredentialItemNotFoundError
from skyvern.forge import app
from skyvern.forge.sdk.schemas.credentials import (
    CreateCredentialRequest,
    Credential,
    CredentialType,
    CredentialVaultType,
    NonEmptyPasswordCredential,
    PasswordCredential,
    TotpType,
)
from skyvern.forge.sdk.services import credentials as onepassword_helpers
from skyvern.forge.sdk.services import onepassword_token_service
from skyvern.forge.sdk.services.credential import onepassword_credential_vault_service as onepassword_vault


def _credential() -> Credential:
    return Credential.model_construct(
        credential_id="cred-1",
        organization_id="org-1",
        name="Imported login",
        vault_type=CredentialVaultType.ONEPASSWORD,
        vault_id="vault-1",
        item_id="item-1",
        credential_type=CredentialType.PASSWORD,
        username="cached-user",
        totp_type=TotpType.NONE,
    )


@pytest.mark.asyncio
async def test_get_item_uses_token_policy_and_stored_vault_and_item(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        app,
        "DATABASE",
        SimpleNamespace(
            organizations=SimpleNamespace(get_valid_org_auth_token=AsyncMock(return_value=None)),
        ),
    )
    monkeypatch.setattr(onepassword_token_service.settings, "OP_SERVICE_ACCOUNT_TOKEN", "instance-token")
    allowlisted = AsyncMock(return_value=True)
    monkeypatch.setattr(app.AGENT_FUNCTION, "is_onepassword_instance_default_allowed", allowlisted)
    monkeypatch.setattr(app.AGENT_FUNCTION, "onepassword_instance_default_policy_mode", lambda: "allowlist")

    item = SimpleNamespace(
        fields=[
            SimpleNamespace(id="username", title="Username", value="vault-user", field_type=ItemFieldType.TEXT),
            SimpleNamespace(
                id="password", title="Password", value="vault-password", field_type=ItemFieldType.CONCEALED
            ),
            SimpleNamespace(id="totp", title="One-Time Password", value="vault-totp", field_type=ItemFieldType.TOTP),
        ],
        websites=[SimpleNamespace(url="https://example.invalid")],
    )
    client = MagicMock()
    client.items.get = AsyncMock(return_value=item)
    with patch.object(
        onepassword_helpers.OnePasswordClient,
        "authenticate",
        new_callable=AsyncMock,
        return_value=client,
    ) as auth:
        result = await onepassword_vault.OnePasswordCredentialVaultService().get_credential_item(_credential())

    allowlisted.assert_awaited_once_with("org-1")
    auth.assert_awaited_once_with(auth="instance-token", integration_name="Skyvern", integration_version="v1.0.0")
    client.items.get.assert_awaited_once_with("vault-1", "item-1")
    assert result.item_id == "item-1"
    assert result.name == "Imported login"
    assert isinstance(result.credential, PasswordCredential)
    assert result.credential.username == "vault-user"
    assert result.credential.password == "vault-password"
    assert result.credential.totp == "vault-totp"
    assert result.login_uris == ["https://example.invalid"]


def test_password_mapping_rejects_an_empty_password_instead_of_using_another_concealed_field() -> None:
    item = SimpleNamespace(
        fields=[
            SimpleNamespace(id="password", title="Password", value="", field_type=ItemFieldType.CONCEALED),
            SimpleNamespace(id="pin", title="PIN", value="unrelated-secret", field_type=ItemFieldType.CONCEALED),
        ],
        websites=[],
    )

    with pytest.raises(CredentialItemNotFoundError, match="password field"):
        onepassword_vault._credential_from_item(_credential(), item)


def test_typed_fields_take_precedence_over_alias_matches() -> None:
    item = SimpleNamespace(
        fields=[
            SimpleNamespace(id="custom-otp", title="OTP", value="wrong-code", field_type=ItemFieldType.TEXT),
            SimpleNamespace(id="totp", title="Authenticator", value="correct-code", field_type=ItemFieldType.TOTP),
        ]
    )

    assert (
        onepassword_vault._field_value(
            item,
            {"totp", "otp", "onetimepassword"},
            ItemFieldType.TOTP,
        )
        == "correct-code"
    )


def test_credit_card_mapping_does_not_use_another_concealed_field_as_cvv() -> None:
    credential = _credential().model_copy(update={"credential_type": CredentialType.CREDIT_CARD, "card_brand": "Visa"})
    item = SimpleNamespace(
        fields=[
            SimpleNamespace(
                id="ccnum", title="Card Number", value="4111111111111111", field_type=ItemFieldType.CREDITCARDNUMBER
            ),
            SimpleNamespace(id="expiry", title="Expiration", value="05/2030", field_type=ItemFieldType.MONTHYEAR),
            SimpleNamespace(id="pin", title="PIN", value="unrelated-secret", field_type=ItemFieldType.CONCEALED),
        ],
        websites=[],
    )

    result = onepassword_vault._credential_from_item(credential, item)

    assert result.credential.card_cvv == ""


def test_credit_card_mapping_parses_compact_month_year() -> None:
    credential = _credential().model_copy(update={"credential_type": CredentialType.CREDIT_CARD, "card_brand": "Visa"})
    item = SimpleNamespace(
        fields=[
            SimpleNamespace(
                id="ccnum", title="Card Number", value="4111111111111111", field_type=ItemFieldType.CREDITCARDNUMBER
            ),
            SimpleNamespace(id="expiry", title="Expiration", value="203005", field_type=ItemFieldType.MONTHYEAR),
        ],
        websites=[],
    )

    result = onepassword_vault._credential_from_item(credential, item)

    assert result.credential.card_exp_month == "05"
    assert result.credential.card_exp_year == "2030"


def test_field_mapping_tolerates_missing_titles() -> None:
    item = SimpleNamespace(
        fields=[
            SimpleNamespace(id="username", title=None, value="vault-user", field_type=ItemFieldType.TEXT),
            SimpleNamespace(
                id="password", title="Password", value="vault-password", field_type=ItemFieldType.CONCEALED
            ),
        ],
        websites=[],
    )

    result = onepassword_vault._credential_from_item(_credential(), item)

    assert result.credential.username == "vault-user"


def test_secret_mapping_rejects_multiple_unlabelled_concealed_fields() -> None:
    credential = _credential().model_copy(update={"credential_type": CredentialType.SECRET})
    item = SimpleNamespace(
        fields=[
            SimpleNamespace(
                id="recovery-code", title="Recovery Code", value="first-secret", field_type=ItemFieldType.CONCEALED
            ),
            SimpleNamespace(id="pin", title="PIN", value="second-secret", field_type=ItemFieldType.CONCEALED),
        ],
        websites=[],
        notes="item note",
    )

    with pytest.raises(CredentialItemNotFoundError, match="multiple concealed fields"):
        onepassword_vault._credential_from_item(credential, item)


@pytest.mark.asyncio
async def test_writes_refuse_secrets_and_delete_only_unlinks_row(monkeypatch: pytest.MonkeyPatch) -> None:
    delete_row = AsyncMock()
    write_rows = AsyncMock()
    monkeypatch.setattr(
        app,
        "DATABASE",
        SimpleNamespace(
            credentials=SimpleNamespace(
                create_credential=write_rows,
                update_credential_vault_data=write_rows,
                delete_credential=delete_row,
            )
        ),
    )
    authenticate = AsyncMock()
    with patch.object(onepassword_helpers.OnePasswordClient, "authenticate", authenticate):
        service = onepassword_vault.OnePasswordCredentialVaultService()
        create_request = CreateCredentialRequest(
            name="Fixture login",
            credential_type=CredentialType.PASSWORD,
            credential=NonEmptyPasswordCredential(username="fixture-user", password="synthetic-secret"),
        )
        with pytest.raises(HTTPException) as create_error:
            await service.create_credential("org-1", create_request)
        with pytest.raises(HTTPException) as update_error:
            await service.update_credential(_credential(), create_request)
        await service.delete_credential(_credential())

    assert create_error.value.status_code == 405
    assert update_error.value.status_code == 405
    delete_row.assert_awaited_once_with("cred-1", "org-1")
    write_rows.assert_not_awaited()
    authenticate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "credential_type, secret_label, fields, expected",
    [
        (
            CredentialType.CREDIT_CARD,
            None,
            [
                SimpleNamespace(
                    id="ccnum", title="Card Number", value="4111111111111111", field_type=ItemFieldType.CREDITCARDNUMBER
                ),
                SimpleNamespace(id="cvv", title="CVV", value="123", field_type=ItemFieldType.CONCEALED),
                SimpleNamespace(id="expiry", title="Expiration", value="05/2030", field_type=ItemFieldType.MONTHYEAR),
                SimpleNamespace(id="type", title="Type", value="Visa", field_type=ItemFieldType.CREDITCARDTYPE),
                SimpleNamespace(
                    id="cardholder", title="Cardholder", value="Fixture User", field_type=ItemFieldType.TEXT
                ),
            ],
            {"card_number": "4111111111111111", "card_exp_month": "05", "card_exp_year": "2030"},
        ),
        (
            CredentialType.SECRET,
            "API Key",
            [
                SimpleNamespace(
                    id="api-key", title="API Key", value="fixture-secret", field_type=ItemFieldType.CONCEALED
                )
            ],
            {"secret_value": "fixture-secret", "secret_label": "API Key"},
        ),
    ],
)
async def test_get_item_maps_non_password_credential_types(
    monkeypatch: pytest.MonkeyPatch,
    credential_type: CredentialType,
    secret_label: str | None,
    fields: list[SimpleNamespace],
    expected: dict[str, str],
) -> None:
    monkeypatch.setattr(
        onepassword_vault,
        "resolve_onepassword_token",
        AsyncMock(return_value=SimpleNamespace(token="fixture-token")),
    )
    monkeypatch.setattr(
        onepassword_vault,
        "fetch_onepassword_item",
        AsyncMock(return_value=SimpleNamespace(fields=fields, websites=[], notes=None)),
    )
    credential = _credential().model_copy(
        update={"credential_type": credential_type, "secret_label": secret_label, "card_brand": "Visa"}
    )

    item = await onepassword_vault.OnePasswordCredentialVaultService().get_credential_item(credential)

    assert all(getattr(item.credential, name) == value for name, value in expected.items())
