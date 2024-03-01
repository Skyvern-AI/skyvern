from typing import TYPE_CHECKING, Any

import structlog

from skyvern.forge.sdk.api.aws import AsyncAWSClient
from skyvern.forge.sdk.workflow.models.parameter import PARAMETER_TYPE, Parameter, ParameterType, WorkflowParameter

if TYPE_CHECKING:
    from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunParameter


LOG = structlog.get_logger()


class ContextManager:
    aws_client: AsyncAWSClient
    parameters: dict[str, PARAMETER_TYPE]
    values: dict[str, Any]

    def __init__(self, workflow_parameter_tuples: list[tuple[WorkflowParameter, "WorkflowRunParameter"]]) -> None:
        self.aws_client = AsyncAWSClient()
        self.parameters = {}
        self.values = {}
        for parameter, run_parameter in workflow_parameter_tuples:
            if parameter.key in self.parameters:
                prev_value = self.parameters[parameter.key]
                new_value = run_parameter.value
                LOG.error(
                    f"Duplicate parameter key {parameter.key} found while initializing context manager, previous value: {prev_value}, new value: {new_value}. Using new value."
                )

            self.parameters[parameter.key] = parameter
            self.values[parameter.key] = run_parameter.value

    async def register_parameter_value(
        self,
        parameter: PARAMETER_TYPE,
    ) -> None:
        if parameter.parameter_type == ParameterType.WORKFLOW:
            LOG.error(f"Workflow parameters are set while initializing context manager. Parameter key: {parameter.key}")
            raise ValueError(
                f"Workflow parameters are set while initializing context manager. Parameter key: {parameter.key}"
            )
        elif parameter.parameter_type == ParameterType.AWS_SECRET:
            secret_value = await self.aws_client.get_secret(parameter.aws_key)
            if secret_value is not None:
                self.values[parameter.key] = secret_value
        else:
            # ContextParameter values will be set within the blocks
            return None

    async def register_block_parameters(
        self,
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

            self.parameters[parameter.key] = parameter
            await self.register_parameter_value(parameter)

    def get_parameter(self, key: str) -> Parameter:
        return self.parameters[key]

    def get_value(self, key: str) -> Any:
        return self.values[key]

    def set_value(self, key: str, value: Any) -> None:
        self.values[key] = value
