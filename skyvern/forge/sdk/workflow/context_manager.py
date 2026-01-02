import copy
from typing import TYPE_CHECKING, Any, Self

import structlog
from jinja2.sandbox import SandboxedEnvironment
from onepassword import ItemFieldType
from onepassword.client import Client as OnePasswordClient

from skyvern.config import settings
from skyvern.exceptions import (
    AzureConfigurationError,
    BitwardenBaseError,
    CredentialParameterNotFoundError,
    SkyvernException,
    WorkflowRunContextNotInitialized,
)
from skyvern.forge import app
from skyvern.forge.sdk.api.aws import AsyncAWSClient
from skyvern.forge.sdk.api.azure import AsyncAzureVaultClient
from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
from skyvern.forge.sdk.schemas.credentials import CredentialVaultType, PasswordCredential
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.schemas.tasks import TaskStatus
from skyvern.forge.sdk.services.bitwarden import BitwardenConstants, BitwardenService
from skyvern.forge.sdk.services.credentials import AzureVaultConstants, OnePasswordConstants, parse_totp_secret
from skyvern.forge.sdk.workflow.exceptions import OutputParameterKeyCollisionError
from skyvern.forge.sdk.workflow.models.parameter import (
    PARAMETER_TYPE,
    AWSSecretParameter,
    AzureSecretParameter,
    AzureVaultCredentialParameter,
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
from skyvern.utils.strings import generate_random_string

if TYPE_CHECKING:
    from skyvern.forge.sdk.workflow.models.workflow import Workflow, WorkflowRunParameter

LOG = structlog.get_logger()

BlockMetadata = dict[str, str | int | float | bool | dict | list | None]

jinja_sandbox_env = SandboxedEnvironment()


class WorkflowRunContext:
    @classmethod
    async def init(
        cls,
        aws_client: AsyncAWSClient,
        organization: Organization,
        workflow_run_id: str,
        workflow_title: str,
        workflow_id: str,
        workflow_permanent_id: str,
        workflow_parameter_tuples: list[tuple[WorkflowParameter, "WorkflowRunParameter"]],
        workflow_output_parameters: list[OutputParameter],
        context_parameters: list[ContextParameter],
        secret_parameters: list[
            AWSSecretParameter
            | BitwardenLoginCredentialParameter
            | BitwardenCreditCardDataParameter
            | BitwardenSensitiveInformationParameter
            | OnePasswordCredentialParameter
            | AzureVaultCredentialParameter
            | CredentialParameter
        ],
        block_outputs: dict[str, Any] | None = None,
        workflow: "Workflow | None" = None,
    ) -> Self:
        # key is label name
        workflow_run_context = cls(
            workflow_title=workflow_title,
            workflow_id=workflow_id,
            workflow_permanent_id=workflow_permanent_id,
            workflow_run_id=workflow_run_id,
            aws_client=aws_client,
            workflow=workflow,
        )

        workflow_run_context.organization_id = organization.organization_id

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

        if block_outputs:
            for label, value in block_outputs.items():
                workflow_run_context.values[f"{label}_output"] = value

        for secret_parameter in secret_parameters:
            if isinstance(secret_parameter, AWSSecretParameter):
                await workflow_run_context.register_aws_secret_parameter_value(secret_parameter)
            elif isinstance(secret_parameter, AzureSecretParameter):
                await workflow_run_context.register_azure_secret_parameter_value(secret_parameter)
            elif isinstance(secret_parameter, CredentialParameter):
                await workflow_run_context.register_credential_parameter_value(secret_parameter, organization)
            elif isinstance(secret_parameter, OnePasswordCredentialParameter):
                await workflow_run_context.register_onepassword_credential_parameter_value(
                    secret_parameter, organization
                )
            elif isinstance(secret_parameter, AzureVaultCredentialParameter):
                await workflow_run_context.register_azure_vault_credential_parameter_value(
                    secret_parameter, organization
                )
            elif isinstance(secret_parameter, BitwardenLoginCredentialParameter):
                await workflow_run_context.register_bitwarden_login_credential_parameter_value(
                    secret_parameter, organization
                )
            elif isinstance(secret_parameter, BitwardenCreditCardDataParameter):
                await workflow_run_context.register_bitwarden_credit_card_data_parameter_value(
                    secret_parameter, organization
                )
            elif isinstance(secret_parameter, BitwardenSensitiveInformationParameter):
                await workflow_run_context.register_bitwarden_sensitive_information_parameter_value(
                    secret_parameter, organization
                )

        for context_parameter in context_parameters:
            # All context parameters will be registered with the context manager during initialization but the values
            # will be calculated and set before and after each block execution
            # values sometimes will be overwritten by the block execution itself
            workflow_run_context.parameters[context_parameter.key] = context_parameter

        # Compute once and cache whether secrets should be included in templates
        workflow_run_context.include_secrets_in_templates = (
            await workflow_run_context._should_include_secrets_in_templates()
        )

        return workflow_run_context

    def __init__(
        self,
        workflow_title: str,
        workflow_id: str,
        workflow_permanent_id: str,
        workflow_run_id: str,
        aws_client: AsyncAWSClient,
        workflow: "Workflow | None" = None,
    ) -> None:
        self.workflow_title = workflow_title
        self.workflow_id = workflow_id
        self.workflow_permanent_id = workflow_permanent_id
        self.workflow_run_id = workflow_run_id
        self.workflow = workflow
        self.blocks_metadata: dict[str, BlockMetadata] = {}
        self.parameters: dict[str, PARAMETER_TYPE] = {}
        self.values: dict[str, Any] = {}
        self.secrets: dict[str, Any] = {}
        self._aws_client = aws_client
        self.organization_id: str | None = None
        self.include_secrets_in_templates: bool = False
        self.credential_totp_identifiers: dict[str, str] = {}

    def set_workflow(self, workflow: "Workflow") -> None:
        """
        Update the cached workflow object in the context.
        This is used when the workflow is fetched from the database as a fallback.
        """
        self.workflow = workflow

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

    def get_block_metadata(self, label: str | None) -> BlockMetadata:
        if label is None:
            label = ""
        return self.blocks_metadata.get(label, BlockMetadata())

    async def _should_include_secrets_in_templates(self) -> bool:
        """
        Check if secrets should be included in template formatting based on experimentation provider.
        This check is done once per workflow run context to avoid repeated calls.
        """
        return await app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached(
            "CODE_BLOCK_ENABLED",
            self.workflow_run_id,
            properties={"organization_id": self.organization_id},
        )

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

    def mask_secrets_in_data(self, data: Any, mask: str = "*****") -> Any:
        """
        Recursively replace any real secret values in data with a mask.
        Used to sanitize HttpRequestBlock output before storing.

        Only masks values that exist in self.secrets (registered credentials).
        """
        if not self.secrets:
            return data

        # Collect all non-empty string secret values
        secret_values = {v for v in self.secrets.values() if isinstance(v, str) and v}

        if not secret_values:
            return data

        if isinstance(data, str):
            result = data
            for secret in secret_values:
                result = result.replace(secret, mask)
            return result
        elif isinstance(data, dict):
            return {k: self.mask_secrets_in_data(v, mask) for k, v in data.items()}
        elif isinstance(data, list):
            return [self.mask_secrets_in_data(item, mask) for item in data]
        return data

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
        return f"placeholder_{generate_random_string()}"

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

    async def _register_credential_parameter_value(
        self,
        credential_id: str,
        parameter: Parameter,
        organization: Organization,
    ) -> None:
        db_credential = await app.DATABASE.get_credential(credential_id, organization_id=organization.organization_id)
        if db_credential is None:
            raise CredentialParameterNotFoundError(credential_id)

        vault_type = db_credential.vault_type or CredentialVaultType.BITWARDEN
        credential_service = app.CREDENTIAL_VAULT_SERVICES.get(vault_type)
        if credential_service is None:
            raise CredentialParameterNotFoundError(credential_id)

        credential_item = await credential_service.get_credential_item(db_credential)
        credential = credential_item.credential

        credential_totp_identifier = db_credential.totp_identifier or getattr(credential, "totp_identifier", None)
        if credential_totp_identifier:
            self.credential_totp_identifiers[parameter.key] = credential_totp_identifier

        self.parameters[parameter.key] = parameter
        self.values[parameter.key] = {
            "context": "These values are placeholders. When you type this in, the real value gets inserted (For security reasons)",
        }
        credential_dict: dict[str, str | None] = credential.model_dump()
        for key, value in credential_dict.items():
            # Exclude totp_type from navigation payload as it's metadata, not input data
            if key == "totp_type":
                continue
            if not value:
                continue
            random_secret_id = self.generate_random_secret_id()
            secret_id = f"{random_secret_id}_{key}"
            self.secrets[secret_id] = value
            self.values[parameter.key][key] = secret_id

        if isinstance(credential, PasswordCredential) and credential.totp:
            random_secret_id = self.generate_random_secret_id()
            totp_secret_id = f"{random_secret_id}_totp"
            self.secrets[totp_secret_id] = BitwardenConstants.TOTP
            totp_secret_value = self.totp_secret_value_key(totp_secret_id)
            self.secrets[totp_secret_value] = parse_totp_secret(credential.totp)
            self.values[parameter.key]["totp"] = totp_secret_id

    def get_credential_totp_identifier(self, parameter_key: str) -> str | None:
        return self.credential_totp_identifiers.get(parameter_key)

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

        # Handle regular credentials from the database
        try:
            await self._register_credential_parameter_value(credential_id, parameter, organization)
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

        await self._register_credential_parameter_value(credential_id, parameter, organization)

    async def register_aws_secret_parameter_value(
        self,
        parameter: AWSSecretParameter,
    ) -> None:
        # If the parameter is an AWS secret, fetch the secret value and store it in the secrets dict
        # The value of the parameter will be the random secret id with format `secret_<uuid>`.
        # We'll replace the random secret id with the actual secret value when we need to use it.
        secret_value = await self._aws_client.get_secret(parameter.aws_key)
        if secret_value is not None:
            random_secret_id = self.generate_random_secret_id()
            self.secrets[random_secret_id] = secret_value
            self.values[parameter.key] = random_secret_id
            self.parameters[parameter.key] = parameter

    async def register_azure_secret_parameter_value(
        self,
        parameter: AzureSecretParameter,
    ) -> None:
        vault_name = settings.AZURE_STORAGE_ACCOUNT_NAME
        if vault_name is None:
            LOG.error("AZURE_STORAGE_ACCOUNT_NAME is not configured, cannot register Azure secret parameter value")
            raise AzureConfigurationError("AZURE_STORAGE_ACCOUNT_NAME is not configured")

        # If the parameter is an Azure secret, fetch the secret value and store it in the secrets dict
        # The value of the parameter will be the random secret id with format `secret_<uuid>`.
        # We'll replace the random secret id with the actual secret value when we need to use it.
        azure_vault_client = app.AZURE_CLIENT_FACTORY.create_default()
        async with azure_vault_client:
            secret_value = await azure_vault_client.get_secret(parameter.azure_key, vault_name)
            if secret_value is not None:
                random_secret_id = self.generate_random_secret_id()
                self.secrets[random_secret_id] = secret_value
                self.values[parameter.key] = random_secret_id
                self.parameters[parameter.key] = parameter

    async def register_onepassword_credential_parameter_value(
        self, parameter: OnePasswordCredentialParameter, organization: Organization
    ) -> None:
        org_auth_token = await app.DATABASE.get_valid_org_auth_token(
            organization.organization_id,
            OrganizationAuthTokenType.onepassword_service_account.value,
        )
        token = settings.OP_SERVICE_ACCOUNT_TOKEN
        if org_auth_token:
            token = org_auth_token.token
        if not token:
            raise ValueError(
                "OP_SERVICE_ACCOUNT_TOKEN environment variable not set and no valid 1Password service account token found. Please go to the settings and add your 1Password service account token."
            )

        client = await OnePasswordClient.authenticate(
            auth=token,
            integration_name="Skyvern",
            integration_version="v1.0.0",
        )
        item_id = self._resolve_required_parameter_value(parameter.item_id, "OnePassword Item ID")
        vault_id = self._resolve_required_parameter_value(parameter.vault_id, "OnePassword Vault ID")
        item = await client.items.get(vault_id, item_id)

        # Check if item is None
        if item is None:
            LOG.error(f"No item found for vault_id:{parameter.vault_id}, item_id:{parameter.item_id}")
            raise ValueError(f"1Password item not found: vault_id:{parameter.vault_id}, item_id:{parameter.item_id}")

        self.parameters[parameter.key] = parameter
        self.values[parameter.key] = {
            "context": "These values are placeholders. When you type this in, the real value gets inserted (For security reasons)",
        }

        # Process all fields generically so it covers passwords and credit cards
        for field in item.fields:
            if not field.value or field.field_type == ItemFieldType.UNSUPPORTED:
                continue

            # ignore irrelevant fields to avoid confusing AI
            if field.id in ["validFrom", "interest", "issuenumber"]:
                continue

            field_type = field.field_type.value.lower()
            if field_type == "totp":
                random_secret_id = self.generate_random_secret_id()
                totp_secret_id = f"{random_secret_id}_totp"
                self.secrets[totp_secret_id] = OnePasswordConstants.TOTP
                totp_secret_value = self.totp_secret_value_key(totp_secret_id)
                self.secrets[totp_secret_value] = parse_totp_secret(field.value)
                self.values[parameter.key]["totp"] = totp_secret_id
            elif field.title and field.title.lower() in ["expire date", "expiry date", "expiration date"]:
                parts = [part.strip() for part in field.value.strip().split("/")]

                if len(parts) == 2:
                    month, year_part = parts
                    month = month.zfill(2)  # ensure '5' becomes '05'

                    if len(year_part) == 4:
                        year = year_part[2:]  # 2025 -> 25
                    else:
                        year = year_part

                    self._add_secret_parameter_value(parameter, "card_exp_month", month)
                    self._add_secret_parameter_value(parameter, "card_exp_year", year)
                    if len(year) == 2:
                        self._add_secret_parameter_value(parameter, "card_exp_mmyy", f"{month}/{year}")
                        self._add_secret_parameter_value(parameter, "card_exp_mmyyyy", f"{month}/20{year}")
                    else:
                        # store the 1password-provided value additionally
                        self._add_secret_parameter_value(parameter, "card_exp", field.value)
                else:
                    # fallback on the 1password-provided value
                    self._add_secret_parameter_value(parameter, "card_exp", field.value)
            else:
                # using more descriptive keys than 1password provides by default
                if field.id == "ccnum":
                    self._add_secret_parameter_value(parameter, "card_number", field.value)
                elif field.id == "cardholder":
                    self._add_secret_parameter_value(parameter, "card_holder_name", field.value)
                elif field.id == "cvv":
                    self._add_secret_parameter_value(parameter, "card_cvv", field.value)
                else:
                    # this will be the username, password or other fields
                    self._add_secret_parameter_value(parameter, field.id.replace(" ", "_"), field.value)

        # Secure Note support
        if item.notes:
            self._add_secret_parameter_value(parameter, "notes", item.notes)

    async def register_bitwarden_login_credential_parameter_value(
        self,
        parameter: BitwardenLoginCredentialParameter,
        organization: Organization,
    ) -> None:
        try:
            # Get the Bitwarden login credentials from AWS secrets
            client_id = settings.BITWARDEN_CLIENT_ID or await self._aws_client.get_secret(
                parameter.bitwarden_client_id_aws_secret_key
            )
            client_secret = settings.BITWARDEN_CLIENT_SECRET or await self._aws_client.get_secret(
                parameter.bitwarden_client_secret_aws_secret_key
            )
            master_password = settings.BITWARDEN_MASTER_PASSWORD or await self._aws_client.get_secret(
                parameter.bitwarden_master_password_aws_secret_key
            )
        except Exception as e:
            LOG.error(f"Failed to get Bitwarden login credentials from AWS secrets. Error: {e}")
            raise e

        if not client_id and not settings.BITWARDEN_EMAIL:
            raise ValueError("Bitwarden client ID not found")
        if not client_secret and not settings.BITWARDEN_EMAIL:
            raise ValueError("Bitwarden client secret not found")
        if not master_password:
            raise ValueError("Bitwarden master password not found")

        url = self._resolve_parameter_value(parameter.url_parameter_key)
        if not url and not parameter.bitwarden_item_id:
            LOG.error(f"URL parameter {parameter.url_parameter_key} not found or has no value")
            raise SkyvernException("URL parameter for Bitwarden login credentials not found or has no value")

        collection_id = self._resolve_parameter_value(parameter.bitwarden_collection_id)
        item_id = self._resolve_parameter_value(parameter.bitwarden_item_id)

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
                    "context": "These values are placeholders. When you type this in, the real value gets inserted (For security reasons)",
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

    async def register_azure_vault_credential_parameter_value(
        self,
        parameter: AzureVaultCredentialParameter,
        organization: Organization,
    ) -> None:
        vault_name = self._resolve_required_parameter_value(parameter.vault_name, "Azure Vault Name")
        username_key = self._resolve_required_parameter_value(parameter.username_key, "Azure Username Key")
        password_key = self._resolve_required_parameter_value(parameter.password_key, "Azure Password Key")

        totp_secret_key = self._resolve_parameter_value(parameter.totp_secret_key)

        async with await self._get_azure_vault_client_for_organization(organization) as azure_vault_client:
            secret_username = await azure_vault_client.get_secret(username_key, vault_name)
            if not secret_username:
                raise ValueError(f"Azure Vault username not found by key: {username_key}")

            secret_password = await azure_vault_client.get_secret(password_key, vault_name)
            if not secret_password:
                raise ValueError(f"Azure Vault password not found by key: {password_key}")

            if totp_secret_key:
                totp_secret = await azure_vault_client.get_secret(totp_secret_key, vault_name)
                if not totp_secret:
                    raise ValueError(f"Azure Vault TOTP not found by key: {totp_secret_key}")
            else:
                totp_secret = None

        if secret_username is not None and secret_password is not None:
            random_secret_id = self.generate_random_secret_id()
            # login secret
            username_secret_id = f"{random_secret_id}_username"
            self.secrets[username_secret_id] = secret_username
            # password secret
            password_secret_id = f"{random_secret_id}_password"
            self.secrets[password_secret_id] = secret_password
            self.values[parameter.key] = {
                "context": "These values are placeholders. When you type this in, the real value gets inserted (For security reasons)",
                "username": username_secret_id,
                "password": password_secret_id,
            }
            self.parameters[parameter.key] = parameter

            if totp_secret:
                totp_secret_id = f"{random_secret_id}_totp"
                self.secrets[totp_secret_id] = AzureVaultConstants.TOTP
                totp_secret_value = self.totp_secret_value_key(totp_secret_id)
                self.secrets[totp_secret_value] = parse_totp_secret(totp_secret)
                self.values[parameter.key]["totp"] = totp_secret_id

    async def register_bitwarden_sensitive_information_parameter_value(
        self,
        parameter: BitwardenSensitiveInformationParameter,
        organization: Organization,
    ) -> None:
        try:
            # Get the Bitwarden login credentials from AWS secrets
            client_id = settings.BITWARDEN_CLIENT_ID or await self._aws_client.get_secret(
                parameter.bitwarden_client_id_aws_secret_key
            )
            client_secret = settings.BITWARDEN_CLIENT_SECRET or await self._aws_client.get_secret(
                parameter.bitwarden_client_secret_aws_secret_key
            )
            master_password = settings.BITWARDEN_MASTER_PASSWORD or await self._aws_client.get_secret(
                parameter.bitwarden_master_password_aws_secret_key
            )
        except Exception as e:
            LOG.error(f"Failed to get Bitwarden login credentials from AWS secrets. Error: {e}")
            raise e

        if not client_id and not settings.BITWARDEN_EMAIL:
            raise ValueError("Bitwarden client ID not found")
        if not client_secret and not settings.BITWARDEN_EMAIL:
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
                self.values[parameter.key] = {
                    "context": "These values are placeholders. When you type this in, the real value gets inserted (For security reasons)",
                }
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
        parameter: BitwardenCreditCardDataParameter,
        organization: Organization,
    ) -> None:
        try:
            # Get the Bitwarden login credentials from AWS secrets
            client_id = settings.BITWARDEN_CLIENT_ID or await self._aws_client.get_secret(
                parameter.bitwarden_client_id_aws_secret_key
            )
            client_secret = settings.BITWARDEN_CLIENT_SECRET or await self._aws_client.get_secret(
                parameter.bitwarden_client_secret_aws_secret_key
            )
            master_password = settings.BITWARDEN_MASTER_PASSWORD or await self._aws_client.get_secret(
                parameter.bitwarden_master_password_aws_secret_key
            )
        except Exception as e:
            LOG.error(f"Failed to get Bitwarden login credentials from AWS secrets. Error: {e}")
            raise e

        if not client_id and not settings.BITWARDEN_EMAIL:
            raise ValueError("Bitwarden client ID not found")
        if not client_secret and not settings.BITWARDEN_EMAIL:
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
            parameter_value["context"] = (
                "These values are placeholders. When you type this in, the real value gets inserted (For security reasons)"
            )

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
                    AzureSecretParameter,
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

    def _resolve_required_parameter_value(self, parameter_value: str | None, name: str) -> str:
        result = self._resolve_parameter_value(parameter_value)
        if not result:
            raise ValueError(f"{name} is missing")
        return result

    def _resolve_parameter_value(self, parameter_value: str | None) -> str | None:
        if not parameter_value:
            return parameter_value

        # Fallback on direct value in case configured as 'my_parameter' instead of '{{ my_parameter }}'
        if self.has_parameter(parameter_value) and self.has_value(parameter_value):
            return self.values[parameter_value]
        else:
            return jinja_sandbox_env.from_string(parameter_value).render(self.values)

    @staticmethod
    async def _get_azure_vault_client_for_organization(organization: Organization) -> AsyncAzureVaultClient:
        org_auth_token = await app.DATABASE.get_valid_org_auth_token(
            organization.organization_id, OrganizationAuthTokenType.azure_client_secret_credential.value
        )
        if org_auth_token:
            azure_vault_client = app.AZURE_CLIENT_FACTORY.create_from_client_secret(org_auth_token.credential)
        else:
            # Use the DefaultAzureCredential if not configured on organization level
            azure_vault_client = app.AZURE_CLIENT_FACTORY.create_default()
        return azure_vault_client

    def _add_secret_parameter_value(self, parameter: Parameter, key: str, value: str) -> None:
        if parameter.key not in self.values:
            raise ValueError(f"{parameter.key} is missing")

        random_secret_id = self.generate_random_secret_id()
        secret_id = f"{random_secret_id}_{key}"
        self.secrets[secret_id] = value
        self.values[parameter.key][key] = secret_id


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
        workflow_title: str,
        workflow_id: str,
        workflow_permanent_id: str,
        workflow_parameter_tuples: list[tuple[WorkflowParameter, "WorkflowRunParameter"]],
        workflow_output_parameters: list[OutputParameter],
        context_parameters: list[ContextParameter],
        secret_parameters: list[
            AWSSecretParameter
            | BitwardenLoginCredentialParameter
            | BitwardenCreditCardDataParameter
            | BitwardenSensitiveInformationParameter
            | OnePasswordCredentialParameter
            | AzureVaultCredentialParameter
            | CredentialParameter
        ],
        block_outputs: dict[str, Any] | None = None,
        workflow: "Workflow | None" = None,
    ) -> WorkflowRunContext:
        workflow_run_context = await WorkflowRunContext.init(
            self.aws_client,
            organization,
            workflow_run_id,
            workflow_title,
            workflow_id,
            workflow_permanent_id,
            workflow_parameter_tuples,
            workflow_output_parameters,
            context_parameters,
            secret_parameters,
            block_outputs,
            workflow,
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
