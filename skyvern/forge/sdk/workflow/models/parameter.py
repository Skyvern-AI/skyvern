import abc
import json
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

from skyvern.exceptions import InvalidWorkflowParameter


class ParameterType(StrEnum):
    WORKFLOW = "workflow"
    CONTEXT = "context"
    AWS_SECRET = "aws_secret"
    BITWARDEN_LOGIN_CREDENTIAL = "bitwarden_login_credential"
    BITWARDEN_SENSITIVE_INFORMATION = "bitwarden_sensitive_information"
    BITWARDEN_CREDIT_CARD_DATA = "bitwarden_credit_card_data"
    OUTPUT = "output"
    CREDENTIAL = "credential"


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
    url_parameter_key: str | None = None
    # bitwarden collection id to filter the login credentials from,
    # if not provided, no filtering will be done
    bitwarden_collection_id: str | None = None
    # bitwarden item id to request the login credential
    bitwarden_item_id: str | None = None

    created_at: datetime
    modified_at: datetime
    deleted_at: datetime | None = None


class CredentialParameter(Parameter):
    model_config = ConfigDict(from_attributes=True)
    parameter_type: Literal[ParameterType.CREDENTIAL] = ParameterType.CREDENTIAL

    credential_parameter_id: str
    workflow_id: str

    credential_id: str

    created_at: datetime
    modified_at: datetime
    deleted_at: datetime | None = None


class BitwardenSensitiveInformationParameter(Parameter):
    parameter_type: Literal[ParameterType.BITWARDEN_SENSITIVE_INFORMATION] = (
        ParameterType.BITWARDEN_SENSITIVE_INFORMATION
    )
    # parameter fields
    bitwarden_sensitive_information_parameter_id: str
    workflow_id: str
    # bitwarden cli required fields
    bitwarden_client_id_aws_secret_key: str
    bitwarden_client_secret_aws_secret_key: str
    bitwarden_master_password_aws_secret_key: str
    # bitwarden collection id to filter the Bitwarden Identity from
    bitwarden_collection_id: str
    # unique key to identify the Bitwarden Identity in the collection
    # this has to be in the identity's name
    bitwarden_identity_key: str
    # fields to extract from the Bitwarden Identity. Custom fields are prioritized over default identity fields
    bitwarden_identity_fields: list[str]

    created_at: datetime
    modified_at: datetime
    deleted_at: datetime | None = None


class BitwardenCreditCardDataParameter(Parameter):
    model_config = ConfigDict(from_attributes=True)
    parameter_type: Literal[ParameterType.BITWARDEN_CREDIT_CARD_DATA] = ParameterType.BITWARDEN_CREDIT_CARD_DATA
    # parameter fields
    bitwarden_credit_card_data_parameter_id: str
    workflow_id: str
    # bitwarden cli required fields
    bitwarden_client_id_aws_secret_key: str
    bitwarden_client_secret_aws_secret_key: str
    bitwarden_master_password_aws_secret_key: str
    # bitwarden ids for the credit card item
    bitwarden_collection_id: str
    bitwarden_item_id: str

    created_at: datetime
    modified_at: datetime
    deleted_at: datetime | None = None


class WorkflowParameterType(StrEnum):
    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    BOOLEAN = "boolean"
    JSON = "json"
    FILE_URL = "file_url"
    CREDENTIAL_ID = "credential_id"

    def convert_value(self, value: Any) -> str | int | float | bool | dict | list | None:
        if value is None:
            return None
        try:
            if self == WorkflowParameterType.STRING:
                return str(value)
            elif self == WorkflowParameterType.INTEGER:
                return int(value)
            elif self == WorkflowParameterType.FLOAT:
                return float(value)
            elif self == WorkflowParameterType.BOOLEAN:
                if isinstance(value, bool):
                    return value
                lower_case = str(value).lower()
                if lower_case not in ["true", "false", "1", "0"]:
                    raise InvalidWorkflowParameter(expected_parameter_type=self, value=str(value))
                return lower_case in ["true", "1"]
            elif self == WorkflowParameterType.JSON:
                return json.loads(value)
            elif self == WorkflowParameterType.FILE_URL:
                return value
            elif self == WorkflowParameterType.CREDENTIAL_ID:
                return value
        except Exception:
            raise InvalidWorkflowParameter(expected_parameter_type=self, value=str(value))


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
    BitwardenSensitiveInformationParameter,
    BitwardenCreditCardDataParameter,
    OutputParameter,
    CredentialParameter,
]
PARAMETER_TYPE = Annotated[ParameterSubclasses, Field(discriminator="parameter_type")]
