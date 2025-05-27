from .bitwarden import BitwardenService
from .onepassword import OnePasswordService
from skyvern.forge.sdk.schemas.credentials import (
    CredentialItem,
    CredentialType,
    PasswordCredential,
)
from skyvern.config import settings


class PasswordManagerService:
    @staticmethod
    async def get_credential_item(item_id: str) -> CredentialItem:
        if settings.PASSWORD_MANAGER.lower() == "onepassword":
            token = settings.ONEPASSWORD_TOKEN or ""
            vault_id = settings.ONEPASSWORD_VAULT or ""
            return CredentialItem(
                item_id=item_id,
                credential_type=CredentialType.PASSWORD,
                name=item_id,
                credential=PasswordCredential(**await OnePasswordService.get_login_item(token, vault_id, item_id)),
            )
        else:
            return await BitwardenService.get_credential_item(item_id)
