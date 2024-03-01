import abc
import json
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field


class ParameterType(StrEnum):
    WORKFLOW = "workflow"
    CONTEXT = "context"
    AWS_SECRET = "aws_secret"


class Parameter(BaseModel, abc.ABC):
    # TODO (kerem): Should we also have organization_id here?
    parameter_type: ParameterType
    key: str
    description: str | None = None

    @classmethod
    def get_subclasses(cls) -> tuple[type["Parameter"], ...]:
        return tuple(cls.__subclasses__())


class AWSSecretParameter(Parameter):
    parameter_type: Literal[ParameterType.AWS_SECRET] = ParameterType.AWS_SECRET

    aws_secret_parameter_id: str
    workflow_id: str
    aws_key: str

    created_at: datetime
    modified_at: datetime
    deleted_at: datetime | None = None


class WorkflowParameterType(StrEnum):
    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    BOOLEAN = "boolean"
    JSON = "json"

    def convert_value(self, value: str | None) -> str | int | float | bool | dict | list | None:
        if value is None:
            return None
        if self == WorkflowParameterType.STRING:
            return value
        elif self == WorkflowParameterType.INTEGER:
            return int(value)
        elif self == WorkflowParameterType.FLOAT:
            return float(value)
        elif self == WorkflowParameterType.BOOLEAN:
            return value.lower() in ["true", "1"]
        elif self == WorkflowParameterType.JSON:
            return json.loads(value)


class WorkflowParameter(Parameter):
    parameter_type: Literal[ParameterType.WORKFLOW] = ParameterType.WORKFLOW

    workflow_parameter_id: str
    workflow_parameter_type: WorkflowParameterType
    workflow_id: str
    # the type of default_value will be determined by the workflow_parameter_type
    default_value: str | int | float | bool | dict | list | None = None

    created_at: datetime
    modified_at: datetime
    deleted_at: datetime | None = None


class ContextParameter(Parameter):
    parameter_type: Literal[ParameterType.CONTEXT] = ParameterType.CONTEXT

    source: WorkflowParameter
    # value will be populated by the context manager
    value: str | int | float | bool | dict | list | None = None


ParameterSubclasses = Union[WorkflowParameter, ContextParameter, AWSSecretParameter]
PARAMETER_TYPE = Annotated[ParameterSubclasses, Field(discriminator="parameter_type")]
