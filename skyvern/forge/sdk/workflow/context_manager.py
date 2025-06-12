import copy
import json
import uuid
from typing import TYPE_CHECKING, Any, Self

import structlog
from onepassword.client import Client as OnePasswordClient

from skyvern.config import settings
from skyvern.exceptions import (
    BitwardenBaseError,
    CredentialParameterNotFoundError,
    SkyvernException,
    WorkflowRunContextNotInitialized,
)
from skyvern.forge import app
from skyvern.forge.sdk.api.aws import AsyncAWSClient
from skyvern.forge.sdk.schemas.credentials import PasswordCredential
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.schemas.tasks import TaskStatus
from skyvern.forge.sdk.services.bitwarden import BitwardenConstants, BitwardenService
from skyvern.forge.sdk.services.credentials import OnePasswordConstants, resolve_secret
from skyvern.forge.sdk.workflow.exceptions import OutputParameterKeyCollisionError
from skyvern.forge.sdk.workflow.models.parameter import (
    PARAMETER_TYPE,
    AWSSecretParameter,
    BitwardenCreditCardDataParameter,
    BitwardenLoginCredentialParameter,
    BitwardenSensitiveInformationParameter,
    ContextParameter,
    CredentialParameter,
    OnePasswordCredentialParameter,
    OutputParameter,
    Parameter,
    ParameterType,
    WorkflowParameter,
    WorkflowParameterType,
)

if TYPE_CHECKING:
    from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunParameter

LOG = structlog.get_logger()

BlockMetadata = dict[str, str | int | float | bool | dict | list]


class WorkflowRunContext:
    @classmethod
    async def init(
        cls,
        aws_client: AsyncAWSClient,
        organization: Organization,
        workflow_parameter_tuples: list[tuple[WorkflowParameter, "WorkflowRunParameter"]],
        workflow_output_parameters: list[OutputParameter],
        context_parameters: list[ContextParameter],
        secret_parameters: list[
            AWSSecretParameter
            | BitwardenLoginCredentialParameter
            | BitwardenCreditCardDataParameter
            | BitwardenSensitiveInformationParameter
            | CredentialParameter
        ],
    ) -> Self:
        # key is label name
        workflow_run_context = cls()
        for parameter, run_parameter in workflow_parameter_tuples:
            if parameter.workflow_parameter_type == WorkflowParameterType.CREDENTIAL_ID:
                await workflow_run_context.register_secret_workflow_parameter_value(
                    parameter, run_parameter.value, organization
                )
                continue
            if parameter.key in workflow_run_context.parameters:
                prev_value = workflow_run_context.parameters[parameter.key]
                new_value = run_parameter.value
                LOG.error(
                    f"Duplicate parameter key {parameter.key} found while initializing context manager, previous value: {prev_value}, new value: {new_value}. Using new value."
                )

            workflow_run_context.parameters[parameter.key] = parameter
            workflow_run_context.values[parameter.key] = run_parameter.value

        for output_parameter in workflow_output_parameters:
            if output_parameter.key in workflow_run_context.parameters:
                raise OutputParameterKeyCollisionError(output_parameter.key)
            workflow_run_context.parameters[output_parameter.key] = output_parameter

        for secrete_parameter in secret_parameters:
            if isinstance(secrete_parameter, AWSSecretParameter):
                await workflow_run_context.register_aws_secret_parameter_value(aws_client, secrete_parameter)
            elif isinstance(secrete_parameter, CredentialParameter):
                await workflow_run_context.register_credential_parameter_value(secrete_parameter, organization)
            elif isinstance(secrete_parameter, OnePasswordCredentialParameter):
                await workflow_run_context.register_onepassword_credential_parameter_value(secrete_parameter)
            elif isinstance(secrete_parameter, BitwardenLoginCredentialParameter):
                await workflow_run_context.register_bitwarden_login_credential_parameter_value(
                    aws_client, secrete_parameter, organization
                )
            elif isinstance(secrete_parameter, BitwardenCreditCardDataParameter):
                await workflow_run_context.register_bitwarden_credit_card_data_parameter_value(
                    aws_client, secrete_parameter, organization
                )
            elif isinstance(secrete_parameter, BitwardenSensitiveInformationParameter):
                await workflow_run_context.register_bitwarden_sensitive_information_parameter_value(
                    aws_client, secrete_parameter, organization
                )

        for context_parameter in context_parameters:
            # All context parameters will be registered with the context manager during initialization but the values
            # will be calculated and set before and after each block execution
            # values sometimes will be overwritten by the block execution itself
            workflow_run_context.parameters[context_parameter.key] = context_parameter

        return workflow_run_context

    def __init__(self) -> None:
        self.blocks_metadata: dict[str, BlockMetadata] = {}
        self.parameters: dict[str, PARAMETER_TYPE] = {}
        self.values: dict[str, Any] = {}
        self.secrets: dict[str, Any] = {}

    def get_parameter(self, key: str) -> Parameter:
        return self.parameters[key]

    def get_value(self, key: str) -> Any:
        """
        Get the value of a parameter. If the parameter is an AWS secret, the value will be the random secret id, not
        the actual secret value. This will be used when building the navigation payload since we don't want to expose
        the actual secret value in the payload.
        """
        return self.values[key]

    def has_parameter(self, key: str) -> bool:
        return key in self.parameters

    def has_value(self, key: str) -> bool:
        return key in self.values

    def set_value(self, key: str, value: Any) -> None:
        self.values[key] = value

    def update_block_metadata(self, label: str, metadata: BlockMetadata) -> None:
        if label in self.blocks_metadata:
            self.blocks_metadata[label].update(metadata)
            return
        self.blocks_metadata[label] = metadata

    def get_block_metadata(self, label: str) -> BlockMetadata:
        return self.blocks_metadata.get(label, BlockMetadata())

    def get_original_secret_value_or_none(self, secret_id_or_value: Any) -> Any:
        """
        Get the original secret value from the secrets dict. If the secret id is not found, return None.

        This function can be called with any possible parameter value, not just the random secret id.

        All the obfuscated secret values are strings, so if the parameter value is a string, we'll assume it's a
        parameter value and return it.

        If the parameter value is a string, it could be a random secret id or an actual parameter value. We'll check if
        the parameter value is a key in the secrets dict. If it is, we'll return the secret value. If it's not, we'll
        assume it's an actual parameter value and return it.

        """
        if isinstance(secret_id_or_value, str):
            return self.secrets.get(secret_id_or_value)
        return None

    async def get_secrets_from_password_manager(self) -> dict[str, Any]:
        """
        Get the secrets from the password manager. The secrets dict will contain the actual secret values.
        """
        secret_credentials = await BitwardenService.get_secret_value_from_url(
            url=self.secrets[BitwardenConstants.URL],
            client_secret=self.secrets[BitwardenConstants.CLIENT_SECRET],
            client_id=self.secrets[BitwardenConstants.CLIENT_ID],
            master_password=self.secrets[BitwardenConstants.MASTER_PASSWORD],
            bw_organization_id=self.secrets[BitwardenConstants.BW_ORGANIZATION_ID],
            bw_collection_ids=self.secrets[BitwardenConstants.BW_COLLECTION_IDS],
            collection_id=self.secrets[BitwardenConstants.BW_COLLECTION_ID],
            item_id=self.secrets[BitwardenConstants.BW_ITEM_ID],
        )
        return secret_credentials

    @staticmethod
    def generate_random_secret_id() -> str:
        return f"secret_{uuid.uuid4()}"

    async def _get_credential_vault_and_item_ids(self, credential_id: str) -> tuple[str, str]:
        """
        Extract vault_id and item_id from the credential_id.
        This method handles the legacy format vault_id:item_id.

        Args:
            credential_id: The credential identifier in the format vault_id:item_id

        Returns:
            A tuple of (vault_id, item_id)

        Raises:
            ValueError: If the credential format is invalid
        """
        # Check if it's in the format vault_id:item_id
        if ":" in credential_id:
            LOG.info(f"Processing credential in vault_id:item_id format: {credential_id}")
            vault_id, item_id = credential_id.split(":", 1)
            return vault_id, item_id

        # If we can't parse the credential_id, raise an error
        raise ValueError(f"Invalid credential format: {credential_id}. Expected format: vault_id:item_id")

    async def register_secret_workflow_parameter_value(
        self,
        parameter: WorkflowParameter,
        value: Any,
        organization: Organization,
    ) -> None:
        credential_id = value

        if not isinstance(credential_id, str):
            raise ValueError(
                f"Trying to register workflow parameter as a secret but it is not a string. Parameter key: {parameter.key}"
            )

        LOG.info(f"Fetching credential parameter value for credential: {credential_id}")

        try:
            # Extract vault_id and item_id from the database
            vault_id, item_id = await self._get_credential_vault_and_item_ids(credential_id)

            # Use the 1Password SDK to resolve the reference using vault_id and item_id directly
            secret_value_json = await resolve_secret(vault_id, item_id)

            # Validate the JSON response
            if not secret_value_json:
                LOG.error(f"Empty response from 1Password for credential: {credential_id}")
                raise ValueError(f"Empty response from 1Password for credential: {credential_id}")

            try:
                secret_values = json.loads(secret_value_json)
            except json.JSONDecodeError as json_err:
                LOG.error(f"Invalid JSON response from 1Password: {secret_value_json[:100]}... Error: {json_err}")
                raise ValueError(f"Invalid JSON response from 1Password: {json_err}")

            if not secret_values:
                LOG.warning(f"No values found in 1Password item: {credential_id}")
                # Still continue with empty values

            self.parameters[parameter.key] = parameter
            self.values[parameter.key] = {}

            # Process fields from the 1Password item
            if "fields" in secret_values and isinstance(secret_values["fields"], list):
                for field in secret_values["fields"]:
                    if not isinstance(field, dict) or "id" not in field or "value" not in field:
                        continue

                    field_id = field.get("id")
                    field_type = field.get("field_type")
                    field_value = field.get("value")

                    # Store the field value
                    random_secret_id = self.generate_random_secret_id()
                    secret_id = f"{random_secret_id}_{field_id}"
                    self.secrets[secret_id] = field_value
                    self.values[parameter.key][field_id] = secret_id

                    # For TOTP fields, also store the current code
                    if field_type == "Totp" and isinstance(field.get("details"), dict):
                        details = field.get("details")
                        # Explicitly check that details is a dict before accessing get method
                        if isinstance(details, dict):
                            content = details.get("content")
                            if isinstance(content, dict) and "code" in content:
                                totp_code = content["code"]
                                random_secret_id = self.generate_random_secret_id()
                                totp_secret_id = f"{random_secret_id}_totp"
                                self.secrets[totp_secret_id] = totp_code
                                totp_secret_value = self.totp_secret_value_key(totp_secret_id)
                                self.secrets[totp_secret_value] = field_value  # Store the TOTP secret
                                self.values[parameter.key]["totp"] = totp_secret_id
            else:
                # Process each field in the 1Password item (old format or custom format)
                for key, value in secret_values.items():
                    random_secret_id = self.generate_random_secret_id()
                    secret_id = f"{random_secret_id}_{key}"
                    self.secrets[secret_id] = value
                    self.values[parameter.key][key] = secret_id

            LOG.info("Successfully processed 1Password credential")
            return

        except Exception as e:
            LOG.error(f"Failed to process 1Password credential: {credential_id}. Error: {str(e)}")
            # Add more context to the error
            raise ValueError(f"Failed to process 1Password credential {credential_id}: {str(e)}") from e

        # Handle regular credentials from the database
        try:
            db_credential = await app.DATABASE.get_credential(
                credential_id, organization_id=organization.organization_id
            )
            if db_credential is None:
                raise CredentialParameterNotFoundError(credential_id)

            bitwarden_credential = await BitwardenService.get_credential_item(db_credential.item_id)

            credential_item = bitwarden_credential.credential

            self.parameters[parameter.key] = parameter
            self.values[parameter.key] = {}
            credential_dict = credential_item.model_dump()
            for key, value in credential_dict.items():
                random_secret_id = self.generate_random_secret_id()
                secret_id = f"{random_secret_id}_{key}"
                self.secrets[secret_id] = value
                self.values[parameter.key][key] = secret_id

            if isinstance(credential_item, PasswordCredential) and credential_item.totp is not None:
                random_secret_id = self.generate_random_secret_id()
                totp_secret_id = f"{random_secret_id}_totp"
                self.secrets[totp_secret_id] = BitwardenConstants.TOTP
                totp_secret_value = self.totp_secret_value_key(totp_secret_id)
                self.secrets[totp_secret_value] = credential_item.totp
                self.values[parameter.key]["totp"] = totp_secret_id
        except Exception as e:
            LOG.error(f"Failed to get credential from database: {credential_id}. Error: {e}")
            raise e

    async def register_credential_parameter_value(
        self,
        parameter: CredentialParameter,
        organization: Organization,
    ) -> None:
        LOG.info(f"Fetching credential parameter value for credential: {parameter.credential_id}")

        credential_id = None
        if parameter.credential_id:
            if self.has_parameter(parameter.credential_id) and self.has_value(parameter.credential_id):
                credential_id = self.values[parameter.credential_id]
            else:
                credential_id = parameter.credential_id

        if credential_id is None:
            LOG.error(f"Credential ID not found for credential: {parameter.credential_id}")
            raise CredentialParameterNotFoundError(parameter.credential_id)

        db_credential = await app.DATABASE.get_credential(credential_id, organization_id=organization.organization_id)
        if db_credential is None:
            raise CredentialParameterNotFoundError(credential_id)

        bitwarden_credential = await BitwardenService.get_credential_item(db_credential.item_id)

        credential_item = bitwarden_credential.credential

        self.parameters[parameter.key] = parameter
        self.values[parameter.key] = {}
        credential_dict = credential_item.model_dump()
        for key, value in credential_dict.items():
            random_secret_id = self.generate_random_secret_id()
            secret_id = f"{random_secret_id}_{key}"
            self.secrets[secret_id] = value
            self.values[parameter.key][key] = secret_id

        if isinstance(credential_item, PasswordCredential) and credential_item.totp is not None:
            random_secret_id = self.generate_random_secret_id()
            totp_secret_id = f"{random_secret_id}_totp"
            self.secrets[totp_secret_id] = BitwardenConstants.TOTP
            totp_secret_value = self.totp_secret_value_key(totp_secret_id)
            self.secrets[totp_secret_value] = credential_item.totp
            self.values[parameter.key]["totp"] = totp_secret_id

    async def register_aws_secret_parameter_value(
        self,
        aws_client: AsyncAWSClient,
        parameter: AWSSecretParameter,
    ) -> None:
        # If the parameter is an AWS secret, fetch the secret value and store it in the secrets dict
        # The value of the parameter will be the random secret id with format `secret_<uuid>`.
        # We'll replace the random secret id with the actual secret value when we need to use it.
        secret_value = await aws_client.get_secret(parameter.aws_key)
        if secret_value is not None:
            random_secret_id = self.generate_random_secret_id()
            self.secrets[random_secret_id] = secret_value
            self.values[parameter.key] = random_secret_id
            self.parameters[parameter.key] = parameter

    async def register_onepassword_credential_parameter_value(self, parameter: OnePasswordCredentialParameter) -> None:
        token = settings.OP_SERVICE_ACCOUNT_TOKEN
        if not token:
            raise ValueError("OP_SERVICE_ACCOUNT_TOKEN environment variable not set")

        client = await OnePasswordClient.authenticate(
            auth=token,
            integration_name="Skyvern",
            integration_version="v1.0.0",
        )

        item = await client.items.get(parameter.vault_id, parameter.item_id)

        # Check if item is None
        if item is None:
            LOG.error(f"No item found for vault_id:{parameter.vault_id}, item_id:{parameter.item_id}")
            raise ValueError(f"1Password item not found: vault_id:{parameter.vault_id}, item_id:{parameter.item_id}")

        self.parameters[parameter.key] = parameter
        self.values[parameter.key] = {}

        # Process all fields
        for field in item.fields:
            if field.value is None:
                continue
            random_secret_id = self.generate_random_secret_id()
            secret_id = f"{random_secret_id}_{field.id}"
            self.secrets[secret_id] = field.value
            key = (field.label or field.id).lower().replace(" ", "_")
            self.values[parameter.key][key] = secret_id

        # Try to get TOTP if available
        try:
            totp = await client.items.get_totp(parameter.vault_id, parameter.item_id)
            if totp:
                # Store the actual TOTP value in a separate secret for internal use
                random_secret_id = self.generate_random_secret_id()
                totp_value_id = f"{random_secret_id}_totp_value"
                self.secrets[totp_value_id] = totp

                # Store the special TOTP constant that the agent will recognize
                totp_secret_id = f"{random_secret_id}_totp"
                self.secrets[totp_secret_id] = OnePasswordConstants.TOTP
                self.values[parameter.key]["totp"] = totp_secret_id

                LOG.info(f"TOTP code available for item {parameter.item_id}")
        except Exception as e:
            # TOTP might not be available for this item, just log and continue
            LOG.debug(f"TOTP not available for item {parameter.item_id}: {str(e)}")

    async def register_bitwarden_login_credential_parameter_value(
        self,
        aws_client: AsyncAWSClient,
        parameter: BitwardenLoginCredentialParameter,
        organization: Organization,
    ) -> None:
        try:
            # Get the Bitwarden login credentials from AWS secrets
            client_id = settings.BITWARDEN_CLIENT_ID or await aws_client.get_secret(
                parameter.bitwarden_client_id_aws_secret_key
            )
            client_secret = settings.BITWARDEN_CLIENT_SECRET or await aws_client.get_secret(
                parameter.bitwarden_client_secret_aws_secret_key
            )
            master_password = settings.BITWARDEN_MASTER_PASSWORD or await aws_client.get_secret(
                parameter.bitwarden_master_password_aws_secret_key
            )
        except Exception as e:
            LOG.error(f"Failed to get Bitwarden login credentials from AWS secrets. Error: {e}")
            raise e

        if not client_id:
            raise ValueError("Bitwarden client ID not found")
        if not client_secret:
            raise ValueError("Bitwarden client secret not found")
        if not master_password:
            raise ValueError("Bitwarden master password not found")

        if (
            parameter.url_parameter_key
            and self.has_parameter(parameter.url_parameter_key)
            and self.has_value(parameter.url_parameter_key)
        ):
            url = self.values[parameter.url_parameter_key]
        elif parameter.url_parameter_key:
            # If a key can't be found within the parameter values dict, assume it's a URL (and not a URL Parameter)
            url = parameter.url_parameter_key
        elif parameter.bitwarden_item_id:
            url = None
        else:
            LOG.error(f"URL parameter {parameter.url_parameter_key} not found or has no value")
            raise SkyvernException("URL parameter for Bitwarden login credentials not found or has no value")

        collection_id = None
        if parameter.bitwarden_collection_id:
            if self.has_parameter(parameter.bitwarden_collection_id) and self.has_value(
                parameter.bitwarden_collection_id
            ):
                collection_id = self.values[parameter.bitwarden_collection_id]
            else:
                collection_id = parameter.bitwarden_collection_id

        item_id = None
        if parameter.bitwarden_item_id:
            if self.has_parameter(parameter.bitwarden_item_id) and self.has_value(parameter.bitwarden_item_id):
                item_id = self.values[parameter.bitwarden_item_id]
            else:
                item_id = parameter.bitwarden_item_id

        try:
            secret_credentials = await BitwardenService.get_secret_value_from_url(
                client_id,
                client_secret,
                master_password,
                organization.bw_organization_id,
                organization.bw_collection_ids,
                url,
                collection_id=collection_id,
                item_id=item_id,
            )
            if secret_credentials:
                self.secrets[BitwardenConstants.BW_ORGANIZATION_ID] = organization.bw_organization_id
                self.secrets[BitwardenConstants.BW_COLLECTION_IDS] = organization.bw_collection_ids
                self.secrets[BitwardenConstants.URL] = url
                self.secrets[BitwardenConstants.CLIENT_SECRET] = client_secret
                self.secrets[BitwardenConstants.CLIENT_ID] = client_id
                self.secrets[BitwardenConstants.MASTER_PASSWORD] = master_password
                self.secrets[BitwardenConstants.BW_COLLECTION_ID] = parameter.bitwarden_collection_id
                self.secrets[BitwardenConstants.BW_ITEM_ID] = item_id

                random_secret_id = self.generate_random_secret_id()
                # username secret
                username_secret_id = f"{random_secret_id}_username"
                self.secrets[username_secret_id] = secret_credentials[BitwardenConstants.USERNAME]
                # password secret
                password_secret_id = f"{random_secret_id}_password"
                self.secrets[password_secret_id] = secret_credentials[BitwardenConstants.PASSWORD]
                self.values[parameter.key] = {
                    "username": username_secret_id,
                    "password": password_secret_id,
                }
                self.parameters[parameter.key] = parameter

                if BitwardenConstants.TOTP in secret_credentials and secret_credentials[BitwardenConstants.TOTP]:
                    totp_secret_id = f"{random_secret_id}_totp"
                    self.secrets[totp_secret_id] = BitwardenConstants.TOTP
                    totp_secret_value = self.totp_secret_value_key(totp_secret_id)
                    self.secrets[totp_secret_value] = secret_credentials[BitwardenConstants.TOTP]
                    self.values[parameter.key]["totp"] = totp_secret_id

        except BitwardenBaseError as e:
            LOG.error(f"Failed to get secret from Bitwarden. Error: {e}")
            raise e

    async def register_bitwarden_sensitive_information_parameter_value(
        self,
        aws_client: AsyncAWSClient,
        parameter: BitwardenSensitiveInformationParameter,
        organization: Organization,
    ) -> None:
        try:
            # Get the Bitwarden login credentials from AWS secrets
            client_id = settings.BITWARDEN_CLIENT_ID or await aws_client.get_secret(
                parameter.bitwarden_client_id_aws_secret_key
            )
            client_secret = settings.BITWARDEN_CLIENT_SECRET or await aws_client.get_secret(
                parameter.bitwarden_client_secret_aws_secret_key
            )
            master_password = settings.BITWARDEN_MASTER_PASSWORD or await aws_client.get_secret(
                parameter.bitwarden_master_password_aws_secret_key
            )
        except Exception as e:
            LOG.error(f"Failed to get Bitwarden login credentials from AWS secrets. Error: {e}")
            raise e

        if not client_id:
            raise ValueError("Bitwarden client ID not found")
        if not client_secret:
            raise ValueError("Bitwarden client secret not found")
        if not master_password:
            raise ValueError("Bitwarden master password not found")

        bitwarden_identity_key = parameter.bitwarden_identity_key
        if self.has_parameter(parameter.bitwarden_identity_key) and self.has_value(parameter.bitwarden_identity_key):
            bitwarden_identity_key = self.values[parameter.bitwarden_identity_key]

        collection_id = parameter.bitwarden_collection_id
        if self.has_parameter(parameter.bitwarden_collection_id) and self.has_value(parameter.bitwarden_collection_id):
            collection_id = self.values[parameter.bitwarden_collection_id]

        try:
            sensitive_values = await BitwardenService.get_sensitive_information_from_identity(
                client_id,
                client_secret,
                master_password,
                organization.bw_organization_id,
                organization.bw_collection_ids,
                collection_id,
                bitwarden_identity_key,
                parameter.bitwarden_identity_fields,
            )
            if sensitive_values:
                self.secrets[BitwardenConstants.BW_ORGANIZATION_ID] = organization.bw_organization_id
                self.secrets[BitwardenConstants.BW_COLLECTION_IDS] = organization.bw_collection_ids
                self.secrets[BitwardenConstants.IDENTITY_KEY] = bitwarden_identity_key
                self.secrets[BitwardenConstants.CLIENT_SECRET] = client_secret
                self.secrets[BitwardenConstants.CLIENT_ID] = client_id
                self.secrets[BitwardenConstants.MASTER_PASSWORD] = master_password
                self.secrets[BitwardenConstants.BW_COLLECTION_ID] = collection_id

                self.parameters[parameter.key] = parameter
                self.values[parameter.key] = {}
                for key, value in sensitive_values.items():
                    random_secret_id = self.generate_random_secret_id()
                    secret_id = f"{random_secret_id}_{key}"
                    self.secrets[secret_id] = value
                    self.values[parameter.key][key] = secret_id

        except BitwardenBaseError as e:
            LOG.error(f"Failed to get sensitive information from Bitwarden. Error: {e}")
            raise e

    async def register_bitwarden_credit_card_data_parameter_value(
        self,
        aws_client: AsyncAWSClient,
        parameter: BitwardenCreditCardDataParameter,
        organization: Organization,
    ) -> None:
        try:
            # Get the Bitwarden login credentials from AWS secrets
            client_id = settings.BITWARDEN_CLIENT_ID or await aws_client.get_secret(
                parameter.bitwarden_client_id_aws_secret_key
            )
            client_secret = settings.BITWARDEN_CLIENT_SECRET or await aws_client.get_secret(
                parameter.bitwarden_client_secret_aws_secret_key
            )
            master_password = settings.BITWARDEN_MASTER_PASSWORD or await aws_client.get_secret(
                parameter.bitwarden_master_password_aws_secret_key
            )
        except Exception as e:
            LOG.error(f"Failed to get Bitwarden login credentials from AWS secrets. Error: {e}")
            raise e

        if not client_id:
            raise ValueError("Bitwarden client ID not found")
        if not client_secret:
            raise ValueError("Bitwarden client secret not found")
        if not master_password:
            raise ValueError("Bitwarden master password not found")

        if self.has_parameter(parameter.bitwarden_item_id) and self.has_value(parameter.bitwarden_item_id):
            item_id = self.values[parameter.bitwarden_item_id]
        else:
            item_id = parameter.bitwarden_item_id

        if self.has_parameter(parameter.bitwarden_collection_id) and self.has_value(parameter.bitwarden_collection_id):
            collection_id = self.values[parameter.bitwarden_collection_id]
        else:
            collection_id = parameter.bitwarden_collection_id

        try:
            credit_card_data = await BitwardenService.get_credit_card_data(
                client_id,
                client_secret,
                master_password,
                organization.bw_organization_id,
                organization.bw_collection_ids,
                collection_id,
                item_id,
            )
            if not credit_card_data:
                raise ValueError("Credit card data not found in Bitwarden")

            self.secrets[BitwardenConstants.CLIENT_ID] = client_id
            self.secrets[BitwardenConstants.CLIENT_SECRET] = client_secret
            self.secrets[BitwardenConstants.MASTER_PASSWORD] = master_password
            self.secrets[BitwardenConstants.BW_ITEM_ID] = item_id

            fields_to_obfuscate = {
                BitwardenConstants.CREDIT_CARD_NUMBER: "card_number",
                BitwardenConstants.CREDIT_CARD_CVV: "card_cvv",
            }

            pass_through_fields = {
                BitwardenConstants.CREDIT_CARD_HOLDER_NAME: "card_holder_name",
                BitwardenConstants.CREDIT_CARD_EXPIRATION_MONTH: "card_exp_month",
                BitwardenConstants.CREDIT_CARD_EXPIRATION_YEAR: "card_exp_year",
                BitwardenConstants.CREDIT_CARD_BRAND: "card_brand",
            }

            parameter_value: dict[str, Any] = {
                field_name: credit_card_data[field_key] for field_key, field_name in pass_through_fields.items()
            }

            for data_key, secret_suffix in fields_to_obfuscate.items():
                random_secret_id = self.generate_random_secret_id()
                secret_id = f"{random_secret_id}_{secret_suffix}"
                self.secrets[secret_id] = credit_card_data[data_key]
                parameter_value[secret_suffix] = secret_id

            self.values[parameter.key] = parameter_value
            self.parameters[parameter.key] = parameter

        except BitwardenBaseError as e:
            LOG.error(f"Failed to get credit card data from Bitwarden. Error: {e}")
            raise e

    async def register_parameter_value(
        self,
        aws_client: AsyncAWSClient,
        parameter: PARAMETER_TYPE,
        organization: Organization,
    ) -> None:
        if parameter.parameter_type == ParameterType.WORKFLOW:
            LOG.error(f"Workflow parameters are set while initializing context manager. Parameter key: {parameter.key}")
            raise ValueError(
                f"Workflow parameters are set while initializing context manager. Parameter key: {parameter.key}"
            )
        elif parameter.parameter_type == ParameterType.OUTPUT:
            LOG.error(f"Output parameters are set after each block execution. Parameter key: {parameter.key}")
            raise ValueError(f"Output parameters are set after each block execution. Parameter key: {parameter.key}")
        elif isinstance(parameter, ContextParameter):
            if isinstance(parameter.source, WorkflowParameter):
                # TODO (kerem): set this while initializing the context manager
                workflow_parameter_value = self.get_value(parameter.source.key)
                if not isinstance(workflow_parameter_value, dict):
                    raise ValueError(f"ContextParameter source value is not a dict. Parameter key: {parameter.key}")
                parameter.value = workflow_parameter_value.get(parameter.source.key)
                self.parameters[parameter.key] = parameter
                self.values[parameter.key] = parameter.value
            elif isinstance(parameter.source, ContextParameter):
                # TODO (kerem): update this anytime the source parameter value changes in values dict
                context_parameter_value = self.get_value(parameter.source.key)
                if not isinstance(context_parameter_value, dict):
                    raise ValueError(f"ContextParameter source value is not a dict. Parameter key: {parameter.key}")
                parameter.value = context_parameter_value.get(parameter.source.key)
                self.parameters[parameter.key] = parameter
                self.values[parameter.key] = parameter.value
            elif isinstance(parameter.source, OutputParameter):
                # We won't set the value of the ContextParameter if the source is an OutputParameter it'll be set in
                # `register_output_parameter_value_post_execution` method
                pass
            else:
                raise NotImplementedError(
                    f"ContextParameter source has to be a WorkflowParameter, ContextParameter, or OutputParameter. "
                    f"{parameter.source.parameter_type} is not supported."
                )
        else:
            raise ValueError(f"Unknown parameter type: {parameter.parameter_type}")

    async def register_output_parameter_value_post_execution(
        self, parameter: OutputParameter, value: dict[str, Any] | list | str | None
    ) -> None:
        if parameter.key in self.values:
            LOG.warning(f"Output parameter {parameter.output_parameter_id} already has a registered value, overwriting")

        self.values[parameter.key] = value
        self.register_block_reference_variable_from_output_parameter(parameter, value)

        await self.set_parameter_values_for_output_parameter_dependent_blocks(parameter, value)

    def register_block_reference_variable_from_output_parameter(
        self,
        parameter: OutputParameter,
        value: dict[str, Any] | list | str | None,
    ) -> None:
        # output parameter key is formatted as `<block_label>_output`
        if not parameter.key.endswith("_output"):
            return
        block_label = parameter.key.removesuffix("_output")

        block_reference_value = copy.deepcopy(value)
        if isinstance(block_reference_value, dict) and "extracted_information" in block_reference_value:
            block_reference_value.update({"output": block_reference_value.get("extracted_information")})

        if block_label in self.values:
            current_value = self.values[block_label]
            # only able to merge the value when the current value and the pending value are both dicts
            if isinstance(current_value, dict) and isinstance(block_reference_value, dict):
                block_reference_value.update(current_value)
            else:
                LOG.warning(f"Parameter {block_label} already has a value in workflow run context, overwriting")

        self.values[block_label] = block_reference_value

    async def set_parameter_values_for_output_parameter_dependent_blocks(
        self,
        output_parameter: OutputParameter,
        value: dict[str, Any] | list | str | None,
    ) -> None:
        for key, parameter in self.parameters.items():
            if (
                isinstance(parameter, ContextParameter)
                and isinstance(parameter.source, OutputParameter)
                and parameter.source.key == output_parameter.key
            ):
                # If task isn't completed, we should skip setting the value
                if (
                    isinstance(value, dict)
                    and "extracted_information" in value
                    and "status" in value
                    and value["status"] != TaskStatus.completed
                ):
                    continue
                if isinstance(value, dict) and "errors" in value and value["errors"]:
                    # Is this the correct way to handle errors from task blocks?
                    LOG.error(
                        f"Output parameter {output_parameter.key} has errors. Setting ContextParameter {parameter.key} value to None"
                    )
                    parameter.value = None
                    self.parameters[parameter.key] = parameter
                    self.values[parameter.key] = parameter.value
                    continue
                value = (
                    value["extracted_information"]
                    if isinstance(value, dict) and "extracted_information" in value
                    else value
                )
                if parameter.value:
                    LOG.warning(
                        f"Context parameter {parameter.key} already has a value, overwriting",
                        old_value=parameter.value,
                        new_value=value,
                    )
                if not isinstance(value, dict) and not isinstance(value, list):
                    raise ValueError(
                        f"ContextParameter can only depend on an OutputParameter with a dict or list value. "
                        f"ContextParameter key: {parameter.key}, "
                        f"OutputParameter key: {output_parameter.key}, "
                        f"OutputParameter value: {value}"
                    )
                if isinstance(value, dict):
                    parameter.value = value.get(parameter.key)
                    self.parameters[parameter.key] = parameter
                    self.values[parameter.key] = parameter.value
                else:
                    parameter.value = value
                    self.parameters[parameter.key] = parameter
                    self.values[parameter.key] = parameter.value

    async def register_block_parameters(
        self,
        aws_client: AsyncAWSClient,
        parameters: list[PARAMETER_TYPE],
        organization: Organization,
    ) -> None:
        # Sort the parameters so that ContextParameter and BitwardenLoginCredentialParameter are processed last
        # ContextParameter should be processed at the end since it requires the source parameter to be set
        # BitwardenLoginCredentialParameter should be processed last since it requires the URL parameter to be set
        # Python's tuple comparison works lexicographically, so we can sort the parameters by their type in a tuple
        parameters.sort(
            key=lambda x: (
                isinstance(x, ContextParameter),
                # This makes sure that ContextParameters witha ContextParameter source are processed after all other
                # ContextParameters
                (isinstance(x.source, ContextParameter) if isinstance(x, ContextParameter) else False),
                isinstance(x, BitwardenLoginCredentialParameter),
            )
        )

        for parameter in parameters:
            if parameter.key in self.parameters:
                LOG.debug(f"Parameter {parameter.key} already registered, skipping")
                continue

            if isinstance(parameter, WorkflowParameter):
                LOG.error(
                    f"Workflow parameter {parameter.key} should have already been set through workflow run parameters"
                )
                raise ValueError(
                    f"Workflow parameter {parameter.key} should have already been set through workflow run parameters"
                )
            elif isinstance(parameter, OutputParameter):
                LOG.error(
                    f"Output parameter {parameter.key} should have already been set through workflow run context init"
                )
                raise ValueError(
                    f"Output parameter {parameter.key} should have already been set through workflow run context init"
                )
            elif isinstance(
                parameter,
                (
                    AWSSecretParameter,
                    BitwardenLoginCredentialParameter,
                    BitwardenCreditCardDataParameter,
                    BitwardenSensitiveInformationParameter,
                    CredentialParameter,
                ),
            ):
                LOG.error(
                    f"SecretParameter {parameter.key} should have already been set through workflow run context init"
                )
                raise ValueError(
                    f"SecretParameter {parameter.key} should have already been set through workflow run context init"
                )

            self.parameters[parameter.key] = parameter
            await self.register_parameter_value(aws_client, parameter, organization)

    def totp_secret_value_key(self, totp_secret_id: str) -> str:
        return f"{totp_secret_id}_value"


class WorkflowContextManager:
    aws_client: AsyncAWSClient
    workflow_run_contexts: dict[str, WorkflowRunContext]

    parameters: dict[str, PARAMETER_TYPE]
    values: dict[str, Any]
    secrets: dict[str, Any]

    def __init__(self) -> None:
        self.aws_client = AsyncAWSClient()
        self.workflow_run_contexts = {}

    def _validate_workflow_run_context(self, workflow_run_id: str) -> None:
        if workflow_run_id not in self.workflow_run_contexts:
            LOG.error(f"WorkflowRunContext not initialized for workflow run {workflow_run_id}")
            raise WorkflowRunContextNotInitialized(workflow_run_id=workflow_run_id)

    async def initialize_workflow_run_context(
        self,
        organization: Organization,
        workflow_run_id: str,
        workflow_parameter_tuples: list[tuple[WorkflowParameter, "WorkflowRunParameter"]],
        workflow_output_parameters: list[OutputParameter],
        context_parameters: list[ContextParameter],
        secret_parameters: list[
            AWSSecretParameter
            | BitwardenLoginCredentialParameter
            | BitwardenCreditCardDataParameter
            | BitwardenSensitiveInformationParameter
        ],
    ) -> WorkflowRunContext:
        workflow_run_context = await WorkflowRunContext.init(
            self.aws_client,
            organization,
            workflow_parameter_tuples,
            workflow_output_parameters,
            context_parameters,
            secret_parameters,
        )
        self.workflow_run_contexts[workflow_run_id] = workflow_run_context
        return workflow_run_context

    def get_workflow_run_context(self, workflow_run_id: str) -> WorkflowRunContext:
        self._validate_workflow_run_context(workflow_run_id)
        return self.workflow_run_contexts[workflow_run_id]

    async def register_block_parameters_for_workflow_run(
        self,
        workflow_run_id: str,
        parameters: list[PARAMETER_TYPE],
        organization: Organization,
    ) -> None:
        self._validate_workflow_run_context(workflow_run_id)
        await self.workflow_run_contexts[workflow_run_id].register_block_parameters(
            self.aws_client, parameters, organization
        )

    def add_context_parameter(self, workflow_run_id: str, context_parameter: ContextParameter) -> None:
        self._validate_workflow_run_context(workflow_run_id)
        self.workflow_run_contexts[workflow_run_id].parameters[context_parameter.key] = context_parameter

    async def set_parameter_values_for_output_parameter_dependent_blocks(
        self,
        workflow_run_id: str,
        output_parameter: OutputParameter,
        value: dict[str, Any] | list | str | None,
    ) -> None:
        self._validate_workflow_run_context(workflow_run_id)
        await self.workflow_run_contexts[workflow_run_id].set_parameter_values_for_output_parameter_dependent_blocks(
            output_parameter,
            value,
        )
