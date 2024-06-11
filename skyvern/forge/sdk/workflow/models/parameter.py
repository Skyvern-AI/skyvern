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
    BITWARDEN_LOGIN_CREDENTIAL = "bitwarden_login_credential"
    OUTPUT = "output"


class Parameter(BaseModel, abc.ABC):
    # TODO (kerem): Should we also have organization_id here?
    parameter_type: ParameterType
    key: str
    description: str | None = None

    def __hash__(self) -> int:
        return hash(self.key)

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


class BitwardenLoginCredentialParameter(Parameter):
    parameter_type: Literal[ParameterType.BITWARDEN_LOGIN_CREDENTIAL] = ParameterType.BITWARDEN_LOGIN_CREDENTIAL
    # parameter fields
    bitwarden_login_credential_parameter_id: str
    workflow_id: str
    # bitwarden cli required fields
    bitwarden_client_id_aws_secret_key: str
    bitwarden_client_secret_aws_secret_key: str
    bitwarden_master_password_aws_secret_key: str
    # url to request the login credentials from bitwarden
    url_parameter_key: str
    # bitwarden collection id to filter the login credentials from,
    # if not provided, no filtering will be done
    bitwarden_collection_id: str | None = None

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

    source: "ParameterSubclasses"
    # value will be populated by the context manager
    value: str | int | float | bool | dict | list | None = None


class OutputParameter(Parameter):
    parameter_type: Literal[ParameterType.OUTPUT] = ParameterType.OUTPUT

    output_parameter_id: str
    workflow_id: str

    created_at: datetime
    modified_at: datetime
    deleted_at: datetime | None = None


ParameterSubclasses = Union[
    WorkflowParameter,
    ContextParameter,
    AWSSecretParameter,
    BitwardenLoginCredentialParameter,
    OutputParameter,
]
PARAMETER_TYPE = Annotated[ParameterSubclasses, Field(discriminator="parameter_type")]
