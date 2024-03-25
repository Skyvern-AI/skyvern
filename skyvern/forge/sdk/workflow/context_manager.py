import uuid
from typing import TYPE_CHECKING, Any

import structlog

from skyvern.exceptions import WorkflowRunContextNotInitialized
from skyvern.forge.sdk.api.aws import AsyncAWSClient
from skyvern.forge.sdk.workflow.exceptions import OutputParameterKeyCollisionError
from skyvern.forge.sdk.workflow.models.parameter import (
    PARAMETER_TYPE,
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
        if type(secret_id_or_value) is str:
            return self.secrets.get(secret_id_or_value)
        return None

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
        elif parameter.parameter_type == ParameterType.CONTEXT:
            # ContextParameter values will be set within the blocks
            return
        else:
            raise ValueError(f"Unknown parameter type: {parameter.parameter_type}")

    async def register_output_parameter_value_post_execution(
        self, parameter: OutputParameter, value: dict[str, Any] | list | str | None
    ) -> None:
        if parameter.key in self.values:
            LOG.error(f"Output parameter {parameter.output_parameter_id} already has a registered value")
            return

        self.values[parameter.key] = value

    async def register_block_parameters(
        self,
        aws_client: AsyncAWSClient,
        parameters: list[PARAMETER_TYPE],
    ) -> None:
        for parameter in parameters:
            if parameter.key in self.parameters:
                LOG.debug(f"Parameter {parameter.key} already registered, skipping")
                continue

            if parameter.parameter_type == ParameterType.WORKFLOW:
                LOG.error(
                    f"Workflow parameter {parameter.key} should have already been set through workflow run parameters"
                )
                raise ValueError(
                    f"Workflow parameter {parameter.key} should have already been set through workflow run parameters"
                )
            elif parameter.parameter_type == ParameterType.OUTPUT:
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
    ) -> WorkflowRunContext:
        workflow_run_context = WorkflowRunContext(workflow_parameter_tuples, workflow_output_parameters)
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
