import asyncio
from abc import ABC, abstractmethod
from typing import Any, Awaitable, Callable

import structlog

from skyvern.forge import app
from skyvern.forge.sdk.schemas.credentials import (
    CreateCredentialRequest,
    Credential,
    CredentialItem,
    CredentialType,
    CredentialVaultType,
    CreditCardBillingAddress,
    CreditCardCredential,
    PasswordCredential,
)

LOG = structlog.get_logger()


class CredentialVaultService(ABC):
    """Abstract interface for credential vault services.

    This interface defines the contract for storing and retrieving credentials
    from different vault providers (e.g., Bitwarden, OnePassword, AWS Secrets Manager).
    """

    async def validate_organization_configuration(self, organization_id: str) -> None:
        """Raise if the organization cannot use this vault, without calling the vault itself.

        Vaults configured process-wide have nothing per-organization to check, so this is a no-op.
        """
        return None

    @abstractmethod
    async def create_credential(self, organization_id: str, data: CreateCredentialRequest) -> Credential:
        """Create a new credential in the vault and database."""

    @abstractmethod
    async def update_credential(self, credential: Credential, data: CreateCredentialRequest) -> Credential:
        """Update an existing credential's vault data. Returns the updated credential."""

    @abstractmethod
    async def delete_credential(self, credential: Credential) -> None:
        """Delete a credential from the vault and database."""

    async def post_delete_credential_item(self, item_id: str, organization_id: str | None = None) -> bool:
        """
        Optional hook for scheduling background cleanup tasks after credential deletion.
        Default implementation does nothing. Override in subclasses as needed.
        """
        return True

    @abstractmethod
    async def get_credential_item(self, db_credential: Credential) -> CredentialItem:
        """Retrieve the full credential data from the vault."""

    async def _enqueue_orphaned_vault_item_cleanup(
        self,
        *,
        organization_id: str,
        item_id: str,
        vault_type: CredentialVaultType,
    ) -> bool:
        try:
            return bool(
                await app.AGENT_FUNCTION.on_credential_item_orphaned(
                    organization_id=organization_id,
                    item_id=item_id,
                    vault_type=vault_type,
                )
            )
        except Exception:
            LOG.error(
                "Durable vault-item cleanup enqueue failed; item requires manual reconciliation",
                organization_id=organization_id,
                item_id=item_id,
                vault_type=vault_type,
                exc_info=True,
            )
            raise

    async def _run_task_to_completion(
        self,
        awaitable: Awaitable[None],
        *,
        suppress_cancellation: bool = False,
        cancellation_failure_message: str | None = None,
        log_context: dict[str, object] | None = None,
    ) -> None:
        task = asyncio.ensure_future(awaitable)
        cancellation: asyncio.CancelledError | None = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                cancellation = cancellation or exc
            except Exception:
                if cancellation is None:
                    raise
                break

        if cancellation is None:
            task.result()
            return

        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            if cancellation_failure_message:
                LOG.error(cancellation_failure_message, **(log_context or {}), exc_info=True)

        if not suppress_cancellation:
            raise cancellation

    async def _reclaim_orphaned_vault_item(
        self,
        *,
        delete: Callable[[], Awaitable[None]],
        organization_id: str,
        item_id: str,
        vault_type: CredentialVaultType,
    ) -> None:
        """Reclaim a just-created vault item after the DB repoint failed or was cancelled.

        Runs under shield so a CancelledError (write-lease renewal cancelling the owner task)
        cannot orphan a secret-bearing item between create and repoint. Falls back to durable
        cleanup so the reaper reclaims the item if the inline delete also fails.
        """

        async def _run() -> None:
            try:
                await delete()
                return
            except Exception:
                LOG.error(
                    "Inline vault-item cleanup failed after DB repoint failure; enqueuing durable cleanup",
                    organization_id=organization_id,
                    item_id=item_id,
                    vault_type=vault_type,
                    exc_info=True,
                )
            try:
                await self._enqueue_orphaned_vault_item_cleanup(
                    organization_id=organization_id,
                    item_id=item_id,
                    vault_type=vault_type,
                )
            except Exception:
                # The original failure or cancellation must win over a failed fallback enqueue.
                pass

        await self._run_task_to_completion(_run(), suppress_cancellation=True)

    async def _preserve_omitted_credit_card_fields(
        self,
        credential: Credential,
        updated_credential: CreditCardCredential,
    ) -> CreditCardCredential:
        updated_fields = updated_credential.model_fields_set
        if {
            "billing_address",
            "billing_email",
            "billing_phone",
            "metadata",
        }.issubset(updated_fields):
            return updated_credential

        existing_item = await self.get_credential_item(credential)
        if not isinstance(existing_item.credential, CreditCardCredential):
            return updated_credential

        preserved_fields: dict[str, object] = {}
        existing_credential = existing_item.credential
        if "billing_address" not in updated_fields:
            preserved_fields["billing_address"] = existing_credential.billing_address
        elif updated_credential.billing_address and existing_credential.billing_address:
            preserved_fields["billing_address"] = self._preserve_omitted_billing_address_fields(
                existing_address=existing_credential.billing_address,
                updated_address=updated_credential.billing_address,
            )

        for field_name in ("billing_email", "billing_phone", "metadata"):
            if field_name not in updated_fields:
                preserved_fields[field_name] = getattr(existing_credential, field_name)

        return updated_credential.model_copy(update=preserved_fields)

    async def _preserve_omitted_password_metadata(
        self,
        credential: Credential,
        updated_credential: PasswordCredential,
    ) -> PasswordCredential:
        if "metadata" in updated_credential.model_fields_set:
            return updated_credential

        existing_item = await self.get_credential_item(credential)
        if not isinstance(existing_item.credential, PasswordCredential):
            return updated_credential

        return updated_credential.model_copy(update={"metadata": existing_item.credential.metadata})

    @staticmethod
    def _preserve_omitted_billing_address_fields(
        existing_address: CreditCardBillingAddress,
        updated_address: CreditCardBillingAddress,
    ) -> CreditCardBillingAddress:
        preserved_fields = {}
        updated_fields = updated_address.model_fields_set
        for field_name in (
            "line1",
            "line2",
            "city",
            "state",
            "state_code",
            "postal_code",
            "country",
            "country_code",
        ):
            preserved_fields[field_name] = (
                getattr(updated_address, field_name)
                if field_name in updated_fields
                else getattr(existing_address, field_name)
            )
        return updated_address.model_copy(update=preserved_fields)

    @staticmethod
    async def _create_db_credential(
        organization_id: str,
        data: CreateCredentialRequest,
        item_id: str,
        vault_type: CredentialVaultType,
    ) -> Credential:
        if data.credential_type == CredentialType.PASSWORD:
            return await app.DATABASE.credentials.create_credential(
                organization_id=organization_id,
                name=data.name,
                vault_type=vault_type,
                item_id=item_id,
                credential_type=data.credential_type,
                username=data.credential.username,
                totp_type=data.credential.totp_type,
                totp_identifier=data.credential.totp_identifier,
                card_last4=None,
                card_brand=None,
                tested_url=data.tested_url,
                proxy_location=data.proxy_location,
                proxy_session_id=data.proxy_session_id,
            )
        elif data.credential_type == CredentialType.CREDIT_CARD:
            return await app.DATABASE.credentials.create_credential(
                organization_id=organization_id,
                name=data.name,
                vault_type=vault_type,
                item_id=item_id,
                credential_type=data.credential_type,
                username=None,
                totp_type="none",
                card_last4=data.credential.card_number[-4:],
                card_brand=data.credential.card_brand,
                totp_identifier=None,
                tested_url=data.tested_url,
                proxy_location=data.proxy_location,
                proxy_session_id=data.proxy_session_id,
            )
        elif data.credential_type == CredentialType.SECRET:
            return await app.DATABASE.credentials.create_credential(
                organization_id=organization_id,
                name=data.name,
                vault_type=vault_type,
                item_id=item_id,
                credential_type=data.credential_type,
                username=None,
                totp_type="none",
                card_last4=None,
                card_brand=None,
                totp_identifier=None,
                secret_label=data.credential.secret_label,
                tested_url=data.tested_url,
                proxy_location=data.proxy_location,
                proxy_session_id=data.proxy_session_id,
            )
        else:
            raise Exception(f"Unsupported credential type: {data.credential_type}")

    @staticmethod
    async def _update_db_credential(
        credential: Credential,
        data: CreateCredentialRequest,
        item_id: str,
    ) -> Credential:
        proxy_kwargs: dict[str, Any] = {}
        if "proxy_location" in data.model_fields_set:
            proxy_kwargs["proxy_location"] = data.proxy_location
        if "proxy_session_id" in data.model_fields_set:
            proxy_kwargs["proxy_session_id"] = data.proxy_session_id
        if data.rotate_proxy_session_id:
            proxy_kwargs["rotate_proxy_session_id"] = True

        if data.credential_type == CredentialType.PASSWORD:
            return await app.DATABASE.credentials.update_credential_vault_data(
                credential_id=credential.credential_id,
                organization_id=credential.organization_id,
                item_id=item_id,
                name=data.name,
                credential_type=data.credential_type,
                username=data.credential.username,
                totp_type=data.credential.totp_type,
                totp_identifier=data.credential.totp_identifier,
                card_last4=None,
                card_brand=None,
                tested_url=data.tested_url,
                **proxy_kwargs,
            )
        elif data.credential_type == CredentialType.CREDIT_CARD:
            return await app.DATABASE.credentials.update_credential_vault_data(
                credential_id=credential.credential_id,
                organization_id=credential.organization_id,
                item_id=item_id,
                name=data.name,
                credential_type=data.credential_type,
                username=None,
                totp_type="none",
                card_last4=data.credential.card_number[-4:],
                card_brand=data.credential.card_brand,
                totp_identifier=None,
                tested_url=data.tested_url,
                **proxy_kwargs,
            )
        elif data.credential_type == CredentialType.SECRET:
            return await app.DATABASE.credentials.update_credential_vault_data(
                credential_id=credential.credential_id,
                organization_id=credential.organization_id,
                item_id=item_id,
                name=data.name,
                credential_type=data.credential_type,
                username=None,
                totp_type="none",
                card_last4=None,
                card_brand=None,
                totp_identifier=None,
                secret_label=data.credential.secret_label,
                tested_url=data.tested_url,
                **proxy_kwargs,
            )
        else:
            raise Exception(f"Unsupported credential type: {data.credential_type}")
