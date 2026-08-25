import json

import structlog
from pydantic import ValidationError

from skyvern.exceptions import SkyvernException, SkyvernHTTPException
from skyvern.forge import app
from skyvern.forge.sdk.api.custom_credential_client import CustomCredentialAPIClient
from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
from skyvern.forge.sdk.forge_log import exception_log_fields
from skyvern.forge.sdk.schemas.credentials import (
    CreateCredentialRequest,
    Credential,
    CredentialItem,
    CredentialType,
    CredentialVaultType,
    CreditCardCredential,
    PasswordCredential,
)
from skyvern.forge.sdk.schemas.organizations import CustomCredentialServiceConfig
from skyvern.forge.sdk.services.credential.credential_vault_service import CredentialVaultService
from skyvern.forge.sdk.services.credentials import safe_error_message

LOG = structlog.get_logger()

CONFIGURATION_INVALID_MESSAGE = (
    "Custom credential service configuration for this organization is invalid. "
    "Re-save the API base URL and API token for the custom credential service."
)


class CustomCredentialConfigurationError(SkyvernException):
    """Raised when custom credential service configuration is invalid or missing."""


class CustomCredentialNotConfiguredError(CustomCredentialConfigurationError, SkyvernHTTPException):
    """Raised when an organization has no custom credential service configured.

    This is an expected refusal rather than a fault: it is logged below ERROR and carries a 4xx so
    routes answer with a client error instead of a 500 that re-triggers error alerting.
    """

    def __init__(self, organization_id: str) -> None:
        super().__init__(f"Custom credential service not configured for organization {organization_id}")


def _log_vault_failure(message: str, error: Exception, **context: object) -> None:
    """Report a vault operation failure once, keeping the expected refusal below ERROR."""
    if isinstance(error, CustomCredentialNotConfiguredError):
        LOG.warning(message, error=str(error), **exception_log_fields(error), **context)
        return
    LOG.error(
        message,
        error=safe_error_message(error),
        exc_info=not isinstance(error, ValidationError),
        **exception_log_fields(error),
        **context,
    )


class CustomCredentialVaultService(CredentialVaultService):
    """Custom credential vault service that uses HTTP API for storing credentials."""

    def __init__(self, client: CustomCredentialAPIClient | None = None):
        """
        Initialize the custom credential vault service.

        Args:
            client: HTTP client for the custom credential API (optional, created dynamically if not provided)
        """
        self._client = client

    async def validate_organization_configuration(self, organization_id: str) -> None:
        try:
            await self._get_client_for_organization(organization_id)
        except Exception as e:
            _log_vault_failure(
                "Failed to validate custom vault configuration",
                e,
                organization_id=organization_id,
            )
            raise

    async def _get_client_for_organization(self, organization_id: str) -> CustomCredentialAPIClient:
        """
        Get or create a CustomCredentialAPIClient for the given organization.

        Args:
            organization_id: ID of the organization

        Returns:
            Configured API client for the organization

        Raises:
            CustomCredentialNotConfiguredError: If the organization has no configuration.
            CustomCredentialConfigurationError: If the stored configuration is unusable.
        """
        # If we have a global client (from environment variables), use it
        if self._client:
            return self._client

        # Otherwise, get organization-specific configuration. Failures are reported by the calling
        # operation so that one request produces one log record.
        auth_token = await app.DATABASE.organizations.get_valid_org_auth_token(
            organization_id=organization_id,
            token_type=OrganizationAuthTokenType.custom_credential_service.value,
        )

        if not auth_token:
            raise CustomCredentialNotConfiguredError(organization_id)

        try:
            config_data = json.loads(auth_token.token)
        except json.JSONDecodeError as e:
            raise CustomCredentialConfigurationError(CONFIGURATION_INVALID_MESSAGE) from e

        try:
            config = CustomCredentialServiceConfig.model_validate(config_data)
        except ValidationError:
            # Pydantic embeds the rejected input, which carries the API token, in the error. Drop the
            # cause so it can never reach a traceback.
            raise CustomCredentialConfigurationError(CONFIGURATION_INVALID_MESSAGE) from None

        if not config.api_base_url.strip() or not config.api_token.strip():
            raise CustomCredentialConfigurationError(CONFIGURATION_INVALID_MESSAGE)

        return CustomCredentialAPIClient(api_base_url=config.api_base_url, api_token=config.api_token)

    async def create_credential(self, organization_id: str, data: CreateCredentialRequest) -> Credential:
        """
        Create a new credential in the custom vault and database.

        Args:
            organization_id: ID of the organization
            data: Request data containing credential information

        Returns:
            The created credential record
        """
        LOG.info(
            "Creating credential in custom vault",
            organization_id=organization_id,
            name=data.name,
            credential_type=data.credential_type,
        )

        try:
            # Get the API client for this organization
            client = await self._get_client_for_organization(organization_id)

            # Create credential in the external API
            item_id = await client.create_credential(
                name=data.name,
                credential=data.credential,
            )

            # Create record in Skyvern database
            try:
                credential = await self._create_db_credential(
                    organization_id=organization_id,
                    data=data,
                    item_id=item_id,
                    vault_type=CredentialVaultType.CUSTOM,
                )
            except Exception:
                # Attempt to clean up the external credential
                LOG.warning(
                    "DB creation failed, attempting to clean up external credential",
                    organization_id=organization_id,
                    item_id=item_id,
                )
                try:
                    await client.delete_credential(item_id)
                except Exception as cleanup_error:
                    LOG.error(
                        "Failed to clean up orphaned external credential",
                        organization_id=organization_id,
                        item_id=item_id,
                        error=str(cleanup_error),
                    )
                raise

            LOG.info(
                "Successfully created credential in custom vault",
                organization_id=organization_id,
                credential_id=credential.credential_id,
                item_id=item_id,
            )

            return credential

        except Exception as e:
            _log_vault_failure(
                "Failed to create credential in custom vault",
                e,
                organization_id=organization_id,
                name=data.name,
                credential_type=data.credential_type,
            )
            raise

    async def update_credential(self, credential: Credential, data: CreateCredentialRequest) -> Credential:
        LOG.info(
            "Updating credential in custom vault",
            organization_id=credential.organization_id,
            credential_id=credential.credential_id,
            name=data.name,
            credential_type=data.credential_type,
        )

        try:
            client = await self._get_client_for_organization(credential.organization_id)
            credential_data = data.credential
            if data.credential_type == CredentialType.PASSWORD and isinstance(credential_data, PasswordCredential):
                credential_data = await self._preserve_omitted_password_fields(
                    credential=credential,
                    updated_credential=credential_data,
                )
            elif data.credential_type == CredentialType.CREDIT_CARD and isinstance(
                credential_data, CreditCardCredential
            ):
                credential_data = await self._preserve_omitted_credit_card_fields(
                    credential=credential,
                    updated_credential=credential_data,
                )

            # Create new credential in the external API
            new_item_id = await client.create_credential(
                name=data.name,
                credential=credential_data,
            )

            # Update DB record to point to the new vault item
            try:
                updated_credential = await self._update_db_credential(
                    credential=credential,
                    data=data,
                    item_id=new_item_id,
                )
            except BaseException:
                LOG.warning(
                    "DB update failed; reclaiming the new external credential",
                    organization_id=credential.organization_id,
                    new_item_id=new_item_id,
                )
                await self._reclaim_orphaned_vault_item(
                    delete=lambda: client.delete_credential(new_item_id),
                    organization_id=credential.organization_id,
                    item_id=new_item_id,
                    vault_type=CredentialVaultType.CUSTOM,
                )
                raise

            LOG.info(
                "Successfully updated credential in custom vault",
                organization_id=credential.organization_id,
                credential_id=credential.credential_id,
                old_item_id=credential.item_id,
                new_item_id=new_item_id,
            )

            return updated_credential

        except Exception as e:
            _log_vault_failure(
                "Failed to update credential in custom vault",
                e,
                organization_id=credential.organization_id,
                credential_id=credential.credential_id,
            )
            raise

    async def post_delete_credential_item(self, item_id: str, organization_id: str | None = None) -> bool:
        """
        Background task to delete the old credential item from the custom vault
        after an update or delete operation.
        """
        try:
            if organization_id is None and self._client is None:
                LOG.warning(
                    "Skipping custom vault cleanup; organization_id is required for per-organization configuration",
                    item_id=item_id,
                    organization_id=organization_id,
                )
                return False

            if self._client is not None:
                client = self._client
            else:
                assert organization_id is not None
                client = await self._get_client_for_organization(organization_id)
            await client.delete_credential(item_id)
            LOG.info(
                "Successfully deleted credential item from custom vault in background",
                organization_id=organization_id,
                item_id=item_id,
            )
            return True
        except Exception as exc:
            LOG.warning(
                "Failed to delete credential item from custom vault in background",
                organization_id=organization_id,
                item_id=item_id,
                error_type=type(exc).__name__,
            )
            return False

    async def delete_credential(self, credential: Credential) -> None:
        """
        Delete a credential from the custom vault and database.

        Args:
            credential: Credential record to delete
        """
        LOG.info(
            "Deleting credential from custom vault",
            organization_id=credential.organization_id,
            credential_id=credential.credential_id,
            item_id=credential.item_id,
        )

        try:
            # Get the API client for this organization
            client = await self._get_client_for_organization(credential.organization_id)

            # Delete from external API first
            await client.delete_credential(credential.item_id)

            # Delete from Skyvern database after successful external deletion
            await app.DATABASE.credentials.delete_credential(credential.credential_id, credential.organization_id)

            LOG.info(
                "Successfully deleted credential from custom vault",
                organization_id=credential.organization_id,
                credential_id=credential.credential_id,
                item_id=credential.item_id,
            )

        except Exception as e:
            _log_vault_failure(
                "Failed to delete credential from custom vault",
                e,
                organization_id=credential.organization_id,
                credential_id=credential.credential_id,
                item_id=credential.item_id,
            )
            raise

    async def get_credential_item(self, db_credential: Credential) -> CredentialItem:
        """
        Retrieve the full credential data from the custom vault.

        Args:
            db_credential: Database credential record

        Returns:
            Full credential data from the vault
        """
        LOG.info(
            "Retrieving credential item from custom vault",
            organization_id=db_credential.organization_id,
            credential_id=db_credential.credential_id,
            item_id=db_credential.item_id,
        )

        try:
            # Get the API client for this organization
            client = await self._get_client_for_organization(db_credential.organization_id)

            credential_item = await client.get_credential(
                credential_id=db_credential.item_id,
                name=db_credential.name,
            )

            LOG.info(
                "Successfully retrieved credential item from custom vault",
                organization_id=db_credential.organization_id,
                credential_id=db_credential.credential_id,
                item_id=db_credential.item_id,
            )

            return credential_item

        except Exception as e:
            _log_vault_failure(
                "Failed to retrieve credential item from custom vault",
                e,
                organization_id=db_credential.organization_id,
                credential_id=db_credential.credential_id,
                item_id=db_credential.item_id,
            )
            raise
