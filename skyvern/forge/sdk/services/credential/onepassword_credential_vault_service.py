from fastapi import HTTPException
from onepassword import ItemFieldType
from onepassword.types import Item

from skyvern.exceptions import CredentialItemNotFoundError, CredentialSourceNotConfiguredError
from skyvern.forge import app
from skyvern.forge.sdk.schemas.credentials import (
    CreateCredentialRequest,
    Credential,
    CredentialItem,
    CredentialType,
    CreditCardCredential,
    PasswordCredential,
    SecretCredential,
    TotpType,
)
from skyvern.forge.sdk.services.credential.credential_vault_service import CredentialVaultService
from skyvern.forge.sdk.services.credentials import fetch_onepassword_item, format_resolved_lookup_context
from skyvern.forge.sdk.services.onepassword_token_service import resolve_onepassword_token


def _normalize_field_name(value: str | None) -> str:
    return "".join(character for character in (value or "").casefold() if character.isalnum())


def _field_value(
    item: Item,
    names: set[str],
    field_type: ItemFieldType | None = None,
) -> str | None:
    if field_type is not None:
        for field in item.fields:
            if field.value and field.field_type == field_type:
                return field.value
    for field in item.fields:
        if field.value and ({_normalize_field_name(field.id), _normalize_field_name(field.title)} & names):
            return field.value
    return None


def _credential_from_item(db_credential: Credential, item: Item) -> CredentialItem:
    if db_credential.credential_type == CredentialType.PASSWORD:
        totp = _field_value(item, {"totp", "otp", "onetimepassword"}, ItemFieldType.TOTP)
        password = next(
            (field.value for field in item.fields if field.value and _normalize_field_name(field.id) == "password"),
            None,
        )
        if password is None:
            raise CredentialItemNotFoundError("1Password item has no password field")
        data = PasswordCredential(
            username=_field_value(item, {"username", "user", "login"}) or db_credential.username or "",
            password=password,
            totp=totp,
            totp_type=TotpType.AUTHENTICATOR if totp else db_credential.totp_type,
            totp_identifier=db_credential.totp_identifier,
        )
    elif db_credential.credential_type == CredentialType.CREDIT_CARD:
        expiration = _field_value(item, {"expiry", "expiration", "expirationdate"}, ItemFieldType.MONTHYEAR)
        if expiration and len(expiration) == 6 and expiration.isdigit():
            expiration_parts = [expiration[4:], expiration[:4]]
        else:
            expiration_parts = expiration.split("/", maxsplit=1) if expiration else []
        card_number = _field_value(item, {"number", "cardnumber", "ccnum"}, ItemFieldType.CREDITCARDNUMBER)
        expiration_month = _field_value(item, {"expirationmonth", "expmonth", "month"}) or (
            expiration_parts[0].strip() if len(expiration_parts) == 2 else ""
        )
        expiration_year = _field_value(item, {"expirationyear", "expyear", "year"}) or (
            expiration_parts[1].strip() if len(expiration_parts) == 2 else ""
        )
        if not card_number or not expiration_month or not expiration_year:
            raise CredentialItemNotFoundError("1Password item is missing required credit-card fields")
        data = CreditCardCredential(
            card_number=card_number,
            card_cvv=_field_value(item, {"cvv", "cvc", "securitycode"}) or "",
            card_exp_month=expiration_month,
            card_exp_year=expiration_year,
            card_brand=_field_value(item, {"type", "brand", "cardtype"}, ItemFieldType.CREDITCARDTYPE)
            or db_credential.card_brand
            or "",
            card_holder_name=_field_value(item, {"cardholder", "cardholdername", "name"}) or "",
        )
    elif db_credential.credential_type == CredentialType.SECRET:
        if db_credential.secret_label:
            secret_value = _field_value(item, {_normalize_field_name(db_credential.secret_label)})
        else:
            secret_value = _field_value(item, {"secret", "password", "token", "apikey"})
            if secret_value is None:
                concealed_fields = [
                    field.value for field in item.fields if field.value and field.field_type == ItemFieldType.CONCEALED
                ]
                if len(concealed_fields) > 1:
                    raise CredentialItemNotFoundError(
                        "1Password item has multiple concealed fields; specify a secret label"
                    )
                secret_value = concealed_fields[0] if concealed_fields else item.notes or None
        if secret_value is None:
            raise CredentialItemNotFoundError(f"1Password item has no secret value for {db_credential.item_id}")
        data = SecretCredential(secret_value=secret_value, secret_label=db_credential.secret_label)
    else:
        raise ValueError(f"Unsupported credential type: {db_credential.credential_type}")

    return CredentialItem(
        item_id=db_credential.item_id,
        name=db_credential.name,
        credential_type=db_credential.credential_type,
        credential=data,
        login_uris=[website.url for website in (item.websites or []) if website.url],
    )


class OnePasswordCredentialVaultService(CredentialVaultService):
    async def create_credential(
        self,
        organization_id: str,
        data: CreateCredentialRequest,
        created_by: str | None = None,
        is_passkey_setup_pending: bool = False,
    ) -> Credential:
        raise HTTPException(status_code=405, detail="Creating 1Password-backed credentials is not supported")

    async def update_credential(self, credential: Credential, data: CreateCredentialRequest) -> Credential:
        raise HTTPException(status_code=405, detail="Updating 1Password-backed credentials is not supported")

    async def delete_credential(self, credential: Credential) -> None:
        await app.DATABASE.credentials.delete_credential(credential.credential_id, credential.organization_id)

    async def get_credential_item(self, db_credential: Credential) -> CredentialItem:
        if not db_credential.vault_id or not db_credential.item_id:
            raise CredentialSourceNotConfiguredError("The 1Password credential is missing its vault or item ID")

        resolution = await resolve_onepassword_token(db_credential.organization_id)
        if resolution.token is None:
            raise CredentialSourceNotConfiguredError(
                "1Password is not configured for this organization. Add a 1Password service account token in Settings."
            )

        lookup_context = format_resolved_lookup_context(
            "1Password", vault_id=db_credential.vault_id, item_id=db_credential.item_id
        )
        item = await fetch_onepassword_item(
            resolution.token,
            db_credential.vault_id,
            db_credential.item_id,
            lookup_context,
        )
        return _credential_from_item(db_credential, item)
