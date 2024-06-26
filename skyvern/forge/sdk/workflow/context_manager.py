import uuid
from typing import TYPE_CHECKING, Any

import structlog

from skyvern.exceptions import BitwardenBaseError, WorkflowRunContextNotInitialized
from skyvern.forge.sdk.api.aws import AsyncAWSClient
from skyvern.forge.sdk.services.bitwarden import BitwardenConstants, BitwardenService
from skyvern.forge.sdk.workflow.exceptions import OutputParameterKeyCollisionError
from skyvern.forge.sdk.workflow.models.parameter import (
    PARAMETER_TYPE,
    BitwardenLoginCredentialParameter,
    ContextParameter,
    OutputParameter,
    Parameter,
    ParameterType,
    WorkflowParameter,
)

if TYPE_CHECKING:
    from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunParameter

LOG = structlog.get_logger()


class WorkflowRunContext:
    parameters: dict[str, PARAMETER_TYPE]
    values: dict[str, Any]
    secrets: dict[str, Any]

    def __init__(
        self,
        workflow_parameter_tuples: list[tuple[WorkflowParameter, "WorkflowRunParameter"]],
        workflow_output_parameters: list[OutputParameter],
        context_parameters: list[ContextParameter],
    ) -> None:
        self.parameters = {}
        self.values = {}
        self.secrets = {}
        for parameter, run_parameter in workflow_parameter_tuples:
            if parameter.key in self.parameters:
                prev_value = self.parameters[parameter.key]
                new_value = run_parameter.value
                LOG.error(
                    f"Duplicate parameter key {parameter.key} found while initializing context manager, previous value: {prev_value}, new value: {new_value}. Using new value."
                )

            self.parameters[parameter.key] = parameter
            self.values[parameter.key] = run_parameter.value

        for output_parameter in workflow_output_parameters:
            if output_parameter.key in self.parameters:
                raise OutputParameterKeyCollisionError(output_parameter.key)
            self.parameters[output_parameter.key] = output_parameter

        for context_parameter in context_parameters:
            # All context parameters will be registered with the context manager during initialization but the values
            # will be calculated and set before and after each block execution
            # values sometimes will be overwritten by the block execution itself
            self.parameters[context_parameter.key] = context_parameter

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

    def get_secrets_from_password_manager(self) -> dict[str, Any]:
        """
        Get the secrets from the password manager. The secrets dict will contain the actual secret values.
        """
        secret_credentials = BitwardenService.get_secret_value_from_url(
            url=self.secrets[BitwardenConstants.URL],
            client_secret=self.secrets[BitwardenConstants.CLIENT_SECRET],
            client_id=self.secrets[BitwardenConstants.CLIENT_ID],
            master_password=self.secrets[BitwardenConstants.MASTER_PASSWORD],
        )
        return secret_credentials

    @staticmethod
    def generate_random_secret_id() -> str:
        return f"secret_{uuid.uuid4()}"

    async def register_parameter_value(
        self,
        aws_client: AsyncAWSClient,
        parameter: PARAMETER_TYPE,
    ) -> None:
        if parameter.parameter_type == ParameterType.WORKFLOW:
            LOG.error(f"Workflow parameters are set while initializing context manager. Parameter key: {parameter.key}")
            raise ValueError(
                f"Workflow parameters are set while initializing context manager. Parameter key: {parameter.key}"
            )
        elif parameter.parameter_type == ParameterType.OUTPUT:
            LOG.error(f"Output parameters are set after each block execution. Parameter key: {parameter.key}")
            raise ValueError(f"Output parameters are set after each block execution. Parameter key: {parameter.key}")
        elif parameter.parameter_type == ParameterType.AWS_SECRET:
            # If the parameter is an AWS secret, fetch the secret value and store it in the secrets dict
            # The value of the parameter will be the random secret id with format `secret_<uuid>`.
            # We'll replace the random secret id with the actual secret value when we need to use it.
            secret_value = await aws_client.get_secret(parameter.aws_key)
            if secret_value is not None:
                random_secret_id = self.generate_random_secret_id()
                self.secrets[random_secret_id] = secret_value
                self.values[parameter.key] = random_secret_id
        elif parameter.parameter_type == ParameterType.BITWARDEN_LOGIN_CREDENTIAL:
            try:
                # Get the Bitwarden login credentials from AWS secrets
                client_id = await aws_client.get_secret(parameter.bitwarden_client_id_aws_secret_key)
                client_secret = await aws_client.get_secret(parameter.bitwarden_client_secret_aws_secret_key)
                master_password = await aws_client.get_secret(parameter.bitwarden_master_password_aws_secret_key)
            except Exception as e:
                LOG.error(f"Failed to get Bitwarden login credentials from AWS secrets. Error: {e}")
                raise e

            if self.has_parameter(parameter.url_parameter_key) and self.has_value(parameter.url_parameter_key):
                url = self.values[parameter.url_parameter_key]
            else:
                LOG.error(f"URL parameter {parameter.url_parameter_key} not found or has no value")
                raise ValueError("URL parameter for Bitwarden login credentials not found or has no value")

            collection_id = None
            if parameter.bitwarden_collection_id:
                if self.has_parameter(parameter.bitwarden_collection_id) and self.has_value(
                    parameter.bitwarden_collection_id
                ):
                    collection_id = self.values[parameter.bitwarden_collection_id]
                else:
                    collection_id = parameter.bitwarden_collection_id

            try:
                secret_credentials = BitwardenService.get_secret_value_from_url(
                    client_id,
                    client_secret,
                    master_password,
                    url,
                    collection_id=collection_id,
                )
                if secret_credentials:
                    self.secrets[BitwardenConstants.URL] = url
                    self.secrets[BitwardenConstants.CLIENT_SECRET] = client_secret
                    self.secrets[BitwardenConstants.CLIENT_ID] = client_id
                    self.secrets[BitwardenConstants.MASTER_PASSWORD] = master_password
                    self.secrets[BitwardenConstants.BW_COLLECTION_ID] = parameter.bitwarden_collection_id

                    random_secret_id = self.generate_random_secret_id()
                    # username secret
                    username_secret_id = f"{random_secret_id}_username"
                    self.secrets[username_secret_id] = secret_credentials[BitwardenConstants.USERNAME]
                    # password secret
                    password_secret_id = f"{random_secret_id}_password"
                    self.secrets[password_secret_id] = secret_credentials[BitwardenConstants.PASSWORD]

                    totp_secret_id = f"{random_secret_id}_totp"
                    self.secrets[totp_secret_id] = BitwardenConstants.TOTP

                    self.values[parameter.key] = {
                        "username": username_secret_id,
                        "password": password_secret_id,
                        "totp": totp_secret_id,
                    }
            except BitwardenBaseError as e:
                LOG.error(f"Failed to get secret from Bitwarden. Error: {e}")
                raise e
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
            LOG.warning(f"Output parameter {parameter.output_parameter_id} already has a registered value")
            return

        self.values[parameter.key] = value
        await self.set_parameter_values_for_output_parameter_dependent_blocks(parameter, value)

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
                if not isinstance(value, dict):
                    raise ValueError(
                        f"ContextParameter can't depend on an OutputParameter with a non-dict value. "
                        f"ContextParameter key: {parameter.key}, "
                        f"OutputParameter key: {output_parameter.key}, "
                        f"OutputParameter value: {value}"
                    )
                parameter.value = value.get(parameter.key)
                self.parameters[parameter.key] = parameter
                self.values[parameter.key] = parameter.value

    async def register_block_parameters(
        self,
        aws_client: AsyncAWSClient,
        parameters: list[PARAMETER_TYPE],
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

            self.parameters[parameter.key] = parameter
            await self.register_parameter_value(aws_client, parameter)


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

    def initialize_workflow_run_context(
        self,
        workflow_run_id: str,
        workflow_parameter_tuples: list[tuple[WorkflowParameter, "WorkflowRunParameter"]],
        workflow_output_parameters: list[OutputParameter],
        context_parameters: list[ContextParameter],
    ) -> WorkflowRunContext:
        workflow_run_context = WorkflowRunContext(
            workflow_parameter_tuples, workflow_output_parameters, context_parameters
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
    ) -> None:
        self._validate_workflow_run_context(workflow_run_id)
        await self.workflow_run_contexts[workflow_run_id].register_block_parameters(self.aws_client, parameters)
