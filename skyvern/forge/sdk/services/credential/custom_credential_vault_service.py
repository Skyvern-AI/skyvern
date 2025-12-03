import json

import structlog

from skyvern.exceptions import SkyvernException
from skyvern.forge import app
from skyvern.forge.sdk.api.custom_credential_client import CustomCredentialAPIClient
from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
from skyvern.forge.sdk.schemas.credentials import (
    CreateCredentialRequest,
    Credential,
    CredentialItem,
    CredentialVaultType,
)
from skyvern.forge.sdk.services.credential.credential_vault_service import CredentialVaultService

LOG = structlog.get_logger()


class CustomCredentialConfigurationError(SkyvernException):
    """Raised when custom credential service configuration is invalid or missing."""


class CustomCredentialVaultService(CredentialVaultService):
    """Custom credential vault service that uses HTTP API for storing credentials."""

    def __init__(self, client: CustomCredentialAPIClient | None = None):
        """
        Initialize the custom credential vault service.

        Args:
            client: HTTP client for the custom credential API (optional, created dynamically if not provided)
        """
        self._client = client

    async def _get_client_for_organization(self, organization_id: str) -> CustomCredentialAPIClient:
        """
        Get or create a CustomCredentialAPIClient for the given organization.

        Args:
            organization_id: ID of the organization

        Returns:
            Configured API client for the organization

        Raises:
            Exception: If no configuration is found for the organization
        """
        # If we have a global client (from environment variables), use it
        if self._client:
            return self._client

        # Otherwise, get organization-specific configuration
        try:
            auth_token = await app.DATABASE.get_valid_org_auth_token(
                organization_id=organization_id,
                token_type=OrganizationAuthTokenType.custom_credential_service.value,
            )

            if not auth_token:
                raise CustomCredentialConfigurationError(
                    f"Custom credential service not configured for organization {organization_id}"
                )

            # Parse the stored configuration
            config_data = json.loads(auth_token.token)

            # Create and return the API client
            return CustomCredentialAPIClient(
                api_base_url=config_data["api_base_url"],
                api_token=config_data["api_token"],
            )

        except json.JSONDecodeError as e:
            LOG.exception(
                "Failed to parse custom credential service configuration",
                organization_id=organization_id,
            )
            raise CustomCredentialConfigurationError(
                f"Invalid custom credential service configuration for organization {organization_id}"
            ) from e
        except Exception:
            LOG.exception(
                "Failed to get custom credential service configuration",
                organization_id=organization_id,
            )
            raise

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
            LOG.error(
                "Failed to create credential in custom vault",
                organization_id=organization_id,
                name=data.name,
                credential_type=data.credential_type,
                error=str(e),
                exc_info=True,
            )
            raise

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
            await app.DATABASE.delete_credential(credential.credential_id, credential.organization_id)

            LOG.info(
                "Successfully deleted credential from custom vault",
                organization_id=credential.organization_id,
                credential_id=credential.credential_id,
                item_id=credential.item_id,
            )

        except Exception as e:
            LOG.error(
                "Failed to delete credential from custom vault",
                organization_id=credential.organization_id,
                credential_id=credential.credential_id,
                item_id=credential.item_id,
                error=str(e),
                exc_info=True,
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
            LOG.error(
                "Failed to retrieve credential item from custom vault",
                organization_id=db_credential.organization_id,
                credential_id=db_credential.credential_id,
                item_id=db_credential.item_id,
                error=str(e),
                exc_info=True,
            )
            raise
