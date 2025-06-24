import uuid
from typing import Any

import structlog
from onepassword.client import Client as OnePasswordClient

from skyvern.config import settings
from skyvern.exceptions import (
    BitwardenBaseError,
    CredentialParameterNotFoundError,
)
from skyvern.forge import app
from skyvern.forge.sdk.api.aws import AsyncAWSClient
from skyvern.forge.sdk.schemas.credentials import PasswordCredential
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.schemas.tasks import TaskCredential
from skyvern.forge.sdk.services.bitwarden import BitwardenConstants, BitwardenService
from skyvern.forge.sdk.services.credentials import OnePasswordConstants

LOG = structlog.get_logger()


class TaskCredentialManager:
    """
    Manages credentials for task execution.
    Similar to WorkflowRunContext but for individual tasks.
    """

    def __init__(self, aws_client: AsyncAWSClient):
        self._aws_client = aws_client
        self.secrets: dict[str, Any] = {}
        self.credential_values: dict[str, Any] = {}

    @staticmethod
    def generate_random_secret_id() -> str:
        return f"secret_{uuid.uuid4()}"

    def totp_secret_value_key(self, totp_secret_id: str) -> str:
        return f"{totp_secret_id}_value"

    async def process_task_credentials(
        self,
        credentials: list[TaskCredential] | None,
        organization: Organization,
    ) -> dict[str, Any]:
        """
        Process all task credentials and return credential data for LLM context.

        Returns:
            Dictionary with credential information organized by credential type
        """
        if not credentials:
            return {}

        processed_credentials = {}

        for i, credential in enumerate(credentials):
            credential_key = f"credential_{i}"

            if credential.credential_type == "skyvern_credential":
                if not credential.credential_id:
                    raise ValueError("credential_id is required for skyvern_credential type")

                credential_data = await self._process_skyvern_credential(credential.credential_id, organization)
                processed_credentials[credential_key] = {"type": "credential", "data": credential_data}

            elif credential.credential_type == "onepassword":
                if not credential.vault_id or not credential.item_id:
                    raise ValueError("vault_id and item_id are required for onepassword type")

                credential_data = await self._process_onepassword_credential(credential.vault_id, credential.item_id)
                processed_credentials[credential_key] = {"type": "credential", "data": credential_data}

            elif credential.credential_type == "bitwarden":
                if not all(
                    [
                        credential.bitwarden_client_id_aws_secret_key,
                        credential.bitwarden_client_secret_aws_secret_key,
                        credential.bitwarden_master_password_aws_secret_key,
                    ]
                ):
                    raise ValueError("Bitwarden AWS secret keys are required for bitwarden type")

                credential_data = await self._process_bitwarden_credential(credential, organization)
                processed_credentials[credential_key] = {"type": "credential", "data": credential_data}
            else:
                raise ValueError(f"Unsupported credential type: {credential.credential_type}")

        return processed_credentials

    async def _process_skyvern_credential(self, credential_id: str, organization: Organization) -> dict[str, str]:
        """Process Skyvern-managed credential."""
        LOG.info(f"Processing Skyvern credential: {credential_id}")

        db_credential = await app.DATABASE.get_credential(credential_id, organization_id=organization.organization_id)
        if db_credential is None:
            raise CredentialParameterNotFoundError(credential_id)

        bitwarden_credential = await BitwardenService.get_credential_item(db_credential.item_id)
        credential_item = bitwarden_credential.credential

        credential_data = {}
        credential_dict = credential_item.model_dump()

        for key, value in credential_dict.items():
            random_secret_id = self.generate_random_secret_id()
            secret_id = f"{random_secret_id}_{key}"
            self.secrets[secret_id] = value
            credential_data[key] = secret_id

        if isinstance(credential_item, PasswordCredential) and credential_item.totp is not None:
            random_secret_id = self.generate_random_secret_id()
            totp_secret_id = f"{random_secret_id}_totp"
            self.secrets[totp_secret_id] = BitwardenConstants.TOTP
            totp_secret_value = self.totp_secret_value_key(totp_secret_id)
            self.secrets[totp_secret_value] = credential_item.totp
            credential_data["totp"] = totp_secret_id

        return credential_data

    async def _process_onepassword_credential(self, vault_id: str, item_id: str) -> dict[str, str]:
        """Process OnePassword credential."""
        LOG.info(f"Processing OnePassword credential: vault_id={vault_id}, item_id={item_id}")

        token = settings.OP_SERVICE_ACCOUNT_TOKEN
        if not token:
            raise ValueError("OP_SERVICE_ACCOUNT_TOKEN environment variable not set")

        client = await OnePasswordClient.authenticate(
            auth=token,
            integration_name="Skyvern",
            integration_version="v1.0.0",
        )

        item = await client.items.get(vault_id, item_id)

        if item is None:
            LOG.error(f"No item found for vault_id:{vault_id}, item_id:{item_id}")
            raise ValueError(f"1Password item not found: vault_id:{vault_id}, item_id:{item_id}")

        credential_data = {}

        # Process all fields
        for field in item.fields:
            if field.value is None:
                continue
            random_secret_id = self.generate_random_secret_id()
            field_type = field.field_type.value.lower()
            if field_type == "totp":
                totp_secret_id = f"{random_secret_id}_totp"
                self.secrets[totp_secret_id] = OnePasswordConstants.TOTP
                totp_secret_value = self.totp_secret_value_key(totp_secret_id)
                self.secrets[totp_secret_value] = field.value
                credential_data["totp"] = totp_secret_id
            else:
                # this will be the username or password or other field
                key = field.id.replace(" ", "_")
                secret_id = f"{random_secret_id}_{key}"
                self.secrets[secret_id] = field.value
                credential_data[key] = secret_id

        return credential_data

    async def _process_bitwarden_credential(
        self, credential: TaskCredential, organization: Organization
    ) -> dict[str, str]:
        """Process Bitwarden credential."""
        LOG.info("Processing Bitwarden credential")

        try:
            # Get the Bitwarden login credentials from AWS secrets
            client_id = settings.BITWARDEN_CLIENT_ID
            if not client_id and credential.bitwarden_client_id_aws_secret_key:
                client_id = await self._aws_client.get_secret(credential.bitwarden_client_id_aws_secret_key)

            client_secret = settings.BITWARDEN_CLIENT_SECRET
            if not client_secret and credential.bitwarden_client_secret_aws_secret_key:
                client_secret = await self._aws_client.get_secret(credential.bitwarden_client_secret_aws_secret_key)

            master_password = settings.BITWARDEN_MASTER_PASSWORD
            if not master_password and credential.bitwarden_master_password_aws_secret_key:
                master_password = await self._aws_client.get_secret(credential.bitwarden_master_password_aws_secret_key)
        except Exception as e:
            LOG.error(f"Failed to get Bitwarden login credentials from AWS secrets. Error: {e}")
            raise e

        if not client_id:
            raise ValueError("Bitwarden client ID not found")
        if not client_secret:
            raise ValueError("Bitwarden client secret not found")
        if not master_password:
            raise ValueError("Bitwarden master password not found")

        try:
            # Use item_id if provided, otherwise fall back to other methods
            if credential.bitwarden_item_id:
                secret_credentials = await BitwardenService.get_secret_value_from_url(
                    client_id=client_id,
                    client_secret=client_secret,
                    master_password=master_password,
                    bw_organization_id=organization.bw_organization_id,
                    bw_collection_ids=organization.bw_collection_ids,
                    url=None,
                    collection_id=credential.bitwarden_collection_id,
                    item_id=credential.bitwarden_item_id,
                )
            else:
                raise ValueError("bitwarden_item_id is required for task credentials")

            if secret_credentials:
                credential_data = {}
                random_secret_id = self.generate_random_secret_id()

                # username secret
                username_secret_id = f"{random_secret_id}_username"
                self.secrets[username_secret_id] = secret_credentials[BitwardenConstants.USERNAME]
                credential_data["username"] = username_secret_id

                # password secret
                password_secret_id = f"{random_secret_id}_password"
                self.secrets[password_secret_id] = secret_credentials[BitwardenConstants.PASSWORD]
                credential_data["password"] = password_secret_id

                # totp secret
                if secret_credentials.get(BitwardenConstants.TOTP):
                    totp_secret_id = f"{random_secret_id}_totp"
                    self.secrets[totp_secret_id] = BitwardenConstants.TOTP
                    totp_secret_value = self.totp_secret_value_key(totp_secret_id)
                    self.secrets[totp_secret_value] = secret_credentials[BitwardenConstants.TOTP]
                    credential_data["totp"] = totp_secret_id

                return credential_data
            else:
                raise ValueError("Bitwarden credentials not found")

        except BitwardenBaseError as e:
            LOG.error(f"Failed to get Bitwarden credentials. Error: {e}")
            raise e

    def get_original_secret_value_or_none(self, secret_id_or_value: Any) -> Any:
        """
        Get the original secret value from the secrets dict. If the secret id is not found, return None.

        This function can be called with any possible parameter value, not just the random secret id.
        """
        if isinstance(secret_id_or_value, str):
            return self.secrets.get(secret_id_or_value)
        return None

    async def build_credential_context(
        self,
        credentials: list[TaskCredential] | None,
        organization: Organization,
    ) -> str:
        """
        Build credential context string for LLM prompts.

        Args:
            credentials: List of task credentials
            organization: Organization context

        Returns:
            Formatted credential context string for LLM
        """
        if not credentials:
            return ""

        try:
            processed_credentials = await self.process_task_credentials(credentials, organization)

            if not processed_credentials:
                return ""

            context_parts = ["Available credentials for this task:"]

            for credential_key, credential_info in processed_credentials.items():
                credential_type = credential_info.get("type", "credential")
                credential_data = credential_info.get("data", {})

                if credential_data:
                    context_parts.append(
                        f"- {credential_type.capitalize()} with fields: {', '.join(credential_data.keys())}"
                    )

            return "\n".join(context_parts)
        except Exception as e:
            LOG.error(f"Failed to build credential context: {e}")
            return ""
