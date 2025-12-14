from typing import Any

import structlog

from skyvern.exceptions import HttpException
from skyvern.forge.sdk.core.aiohttp_helper import aiohttp_delete, aiohttp_get_json, aiohttp_post
from skyvern.forge.sdk.schemas.credentials import (
    CredentialItem,
    CredentialType,
    CreditCardCredential,
    PasswordCredential,
    SecretCredential,
)

LOG = structlog.get_logger()


class CustomCredentialAPIClient:
    """HTTP client for interacting with custom credential service APIs."""

    def __init__(self, api_base_url: str, api_token: str):
        """
        Initialize the custom credential API client.

        Args:
            api_base_url: Base URL for the custom credential API
            api_token: Bearer token for authentication
        """
        self.api_base_url = api_base_url.rstrip("/")
        self.api_token = api_token

    def _get_auth_headers(self) -> dict[str, str]:
        """Get headers for API authentication."""
        return {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json",
        }

    def _credential_to_api_payload(
        self, credential: PasswordCredential | CreditCardCredential | SecretCredential
    ) -> dict[str, Any]:
        """Convert Skyvern credential to API payload format."""
        if isinstance(credential, PasswordCredential):
            return {
                "type": "password",
                "username": credential.username,
                "password": credential.password,
                "totp": credential.totp,
                "totp_type": credential.totp_type,
            }
        elif isinstance(credential, CreditCardCredential):
            return {
                "type": "credit_card",
                "card_holder_name": credential.card_holder_name,
                "card_number": credential.card_number,
                "card_exp_month": credential.card_exp_month,
                "card_exp_year": credential.card_exp_year,
                "card_cvv": credential.card_cvv,
                "card_brand": credential.card_brand,
            }
        elif isinstance(credential, SecretCredential):
            payload = {
                "type": "secret",
                "secret_value": credential.secret_value,
            }
            if credential.secret_label is not None:
                payload["secret_label"] = credential.secret_label
            return payload
        else:
            raise TypeError(f"Unsupported credential type: {type(credential)}")

    def _api_response_to_credential(self, credential_data: dict[str, Any], name: str, item_id: str) -> CredentialItem:
        """Convert API response to Skyvern CredentialItem."""
        credential_type = credential_data.get("type")

        if credential_type == "password":
            required_fields = ["username", "password"]
            missing = [f for f in required_fields if f not in credential_data]
            if missing:
                raise ValueError(f"Missing required password fields from API: {missing}")

            credential = PasswordCredential(
                username=credential_data["username"],
                password=credential_data["password"],
                totp=credential_data.get("totp"),
                totp_type=credential_data.get("totp_type", "none"),
            )
            return CredentialItem(
                item_id=item_id,
                credential=credential,
                name=name,
                credential_type=CredentialType.PASSWORD,
            )
        elif credential_type == "credit_card":
            required_fields = [
                "card_holder_name",
                "card_number",
                "card_exp_month",
                "card_exp_year",
                "card_cvv",
                "card_brand",
            ]
            missing = [f for f in required_fields if f not in credential_data]
            if missing:
                raise ValueError(f"Missing required credit card fields from API: {missing}")

            credential = CreditCardCredential(
                card_holder_name=credential_data["card_holder_name"],
                card_number=credential_data["card_number"],
                card_exp_month=credential_data["card_exp_month"],
                card_exp_year=credential_data["card_exp_year"],
                card_cvv=credential_data["card_cvv"],
                card_brand=credential_data["card_brand"],
            )
            return CredentialItem(
                item_id=item_id,
                credential=credential,
                name=name,
                credential_type=CredentialType.CREDIT_CARD,
            )
        elif credential_type == "secret":
            required_fields = ["secret_value"]
            missing = [f for f in required_fields if f not in credential_data]
            if missing:
                raise ValueError(f"Missing required secret fields from API: {missing}")

            credential = SecretCredential(
                secret_value=credential_data["secret_value"],
                secret_label=credential_data.get("secret_label"),
            )
            return CredentialItem(
                item_id=item_id,
                credential=credential,
                name=name,
                credential_type=CredentialType.SECRET,
            )
        else:
            raise ValueError(f"Unsupported credential type from API: {credential_type}")

    async def create_credential(
        self, name: str, credential: PasswordCredential | CreditCardCredential | SecretCredential
    ) -> str:
        """
        Create a credential using the custom API.

        Args:
            name: Name of the credential
            credential: Credential data to store

        Returns:
            The credential ID returned by the API

        Raises:
            HttpException: If the API request fails
        """
        url = f"{self.api_base_url}"
        headers = self._get_auth_headers()

        payload = {
            "name": name,
            **self._credential_to_api_payload(credential),
        }

        LOG.info(
            "Creating credential via custom API",
            url=url,
            name=name,
            credential_type=type(credential).__name__,
        )

        try:
            response = await aiohttp_post(
                url=url,
                data=payload,
                headers=headers,
                raise_exception=True,
            )

            if not response:
                raise HttpException(500, url, "Empty response from custom credential API")

            # Extract credential ID from response
            credential_id = response.get("id")
            if not credential_id:
                LOG.error(
                    "Custom credential API response missing id field",
                    url=url,
                    response=response,
                )
                raise HttpException(500, url, "Invalid response format from custom credential API")

            LOG.info(
                "Successfully created credential via custom API",
                url=url,
                name=name,
                credential_id=credential_id,
            )

            return str(credential_id)

        except HttpException:
            raise
        except Exception as e:
            LOG.error(
                "Failed to create credential via custom API",
                url=url,
                name=name,
                error=str(e),
                exc_info=True,
            )
            raise HttpException(500, url, f"Failed to create credential via custom API: {e!s}") from e

    async def get_credential(self, credential_id: str, name: str) -> CredentialItem:
        """
        Get a credential using the custom API.

        Args:
            credential_id: ID of the credential to retrieve
            name: Name of the credential (for constructing CredentialItem)

        Returns:
            The credential data

        Raises:
            HttpException: If the API request fails
        """
        url = f"{self.api_base_url}/{credential_id}"
        headers = self._get_auth_headers()

        LOG.info(
            "Retrieving credential via custom API",
            url=url,
            credential_id=credential_id,
        )

        try:
            response = await aiohttp_get_json(
                url=url,
                headers=headers,
                raise_exception=True,
            )

            if not response:
                raise HttpException(404, url, f"Credential not found: {credential_id}")

            LOG.info(
                "Successfully retrieved credential via custom API",
                url=url,
                credential_id=credential_id,
            )

            return self._api_response_to_credential(response, name, credential_id)

        except HttpException:
            raise
        except Exception as e:
            LOG.error(
                "Failed to retrieve credential via custom API",
                url=url,
                credential_id=credential_id,
                error=str(e),
                exc_info=True,
            )
            raise HttpException(500, url, f"Failed to retrieve credential via custom API: {e!s}") from e

    async def delete_credential(self, credential_id: str) -> None:
        """
        Delete a credential using the custom API.

        Args:
            credential_id: ID of the credential to delete

        Raises:
            HttpException: If the API request fails
        """
        url = f"{self.api_base_url}/{credential_id}"
        headers = self._get_auth_headers()

        LOG.info(
            "Deleting credential via custom API",
            url=url,
            credential_id=credential_id,
        )

        try:
            await aiohttp_delete(
                url=url,
                headers=headers,
                raise_exception=True,
            )

            LOG.info(
                "Successfully deleted credential via custom API",
                url=url,
                credential_id=credential_id,
            )

        except HttpException:
            raise
        except Exception as e:
            LOG.error(
                "Failed to delete credential via custom API",
                url=url,
                credential_id=credential_id,
                error=str(e),
                exc_info=True,
            )
            raise HttpException(500, url, f"Failed to delete credential via custom API: {e!s}") from e
