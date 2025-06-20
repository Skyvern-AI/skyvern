import typing
from typing import Any, Union

import typing_extensions
from pydantic import Field

from ..core.universal_base_model import UniversalBaseModel


class HttpBlockParametersItem_AwsSecret(UniversalBaseModel):
    parameter_type: typing_extensions.Literal["aws_secret"] = Field(alias="parameterType", default="aws_secret")
    key: str
    description: typing.Optional[str] = None
    aws_secret_parameter_id: str = Field(alias="awsSecretParameterId")
    aws_key: str = Field(alias="awsKey")
    created_at: str = Field(alias="createdAt")
    modified_at: str = Field(alias="modifiedAt")
    deleted_at: typing.Optional[str] = Field(alias="deletedAt", default=None)
    workflow_id: str = Field(alias="workflowId")

    if typing.TYPE_CHECKING:
        # Pydantic v2
        model_config: typing.ClassVar[pydantic.ConfigDict] = pydantic.ConfigDict(
            extra="allow", populate_by_name=True
        )  # type: ignore[misc]
    else:
        # Pydantic v1
        class Config:
            extra = pydantic.Extra.allow
            allow_population_by_field_name = True
            smart_union = True


class HttpBlockParametersItem_BitwardenCreditCardData(UniversalBaseModel):
    parameter_type: typing_extensions.Literal["bitwarden_credit_card_data"] = Field(
        alias="parameterType", default="bitwarden_credit_card_data"
    )
    key: str
    description: typing.Optional[str] = None
    bitwarden_credit_card_data_parameter_id: str = Field(alias="bitwardenCreditCardDataParameterId")
    bitwarden_client_id_aws_secret_key: str = Field(alias="bitwardenClientIdAwsSecretKey")
    bitwarden_client_secret_aws_secret_key: str = Field(alias="bitwardenClientSecretAwsSecretKey")
    bitwarden_master_password_aws_secret_key: str = Field(alias="bitwardenMasterPasswordAwsSecretKey")
    bitwarden_collection_id: str = Field(alias="bitwardenCollectionId")
    bitwarden_item_id: str = Field(alias="bitwardenItemId")
    created_at: str = Field(alias="createdAt")
    modified_at: str = Field(alias="modifiedAt")
    deleted_at: typing.Optional[str] = Field(alias="deletedAt", default=None)
    workflow_id: str = Field(alias="workflowId")

    if typing.TYPE_CHECKING:
        # Pydantic v2
        model_config: typing.ClassVar[pydantic.ConfigDict] = pydantic.ConfigDict(
            extra="allow", populate_by_name=True
        )  # type: ignore[misc]
    else:
        # Pydantic v1
        class Config:
            extra = pydantic.Extra.allow
            allow_population_by_field_name = True
            smart_union = True


class HttpBlockParametersItem_BitwardenLoginCredential(UniversalBaseModel):
    parameter_type: typing_extensions.Literal["bitwarden_login_credential"] = Field(
        alias="parameterType", default="bitwarden_login_credential"
    )
    key: str
    description: typing.Optional[str] = None
    bitwarden_login_credential_parameter_id: str = Field(alias="bitwardenLoginCredentialParameterId")
    bitwarden_client_id_aws_secret_key: str = Field(alias="bitwardenClientIdAwsSecretKey")
    bitwarden_client_secret_aws_secret_key: str = Field(alias="bitwardenClientSecretAwsSecretKey")
    bitwarden_master_password_aws_secret_key: str = Field(alias="bitwardenMasterPasswordAwsSecretKey")
    bitwarden_collection_id: typing.Optional[str] = Field(alias="bitwardenCollectionId", default=None)
    bitwarden_item_id: typing.Optional[str] = Field(alias="bitwardenItemId", default=None)
    url_parameter_key: typing.Optional[str] = Field(alias="urlParameterKey", default=None)
    created_at: str = Field(alias="createdAt")
    modified_at: str = Field(alias="modifiedAt")
    deleted_at: typing.Optional[str] = Field(alias="deletedAt", default=None)
    workflow_id: str = Field(alias="workflowId")

    if typing.TYPE_CHECKING:
        # Pydantic v2
        model_config: typing.ClassVar[pydantic.ConfigDict] = pydantic.ConfigDict(
            extra="allow", populate_by_name=True
        )  # type: ignore[misc]
    else:
        # Pydantic v1
        class Config:
            extra = pydantic.Extra.allow
            allow_population_by_field_name = True
            smart_union = True


class HttpBlockParametersItem_BitwardenSensitiveInformation(UniversalBaseModel):
    parameter_type: typing_extensions.Literal["bitwarden_sensitive_information"] = Field(
        alias="parameterType", default="bitwarden_sensitive_information"
    )
    key: str
    description: typing.Optional[str] = None
    bitwarden_sensitive_information_parameter_id: str = Field(alias="bitwardenSensitiveInformationParameterId")
    bitwarden_client_id_aws_secret_key: str = Field(alias="bitwardenClientIdAwsSecretKey")
    bitwarden_client_secret_aws_secret_key: str = Field(alias="bitwardenClientSecretAwsSecretKey")
    bitwarden_master_password_aws_secret_key: str = Field(alias="bitwardenMasterPasswordAwsSecretKey")
    bitwarden_collection_id: str = Field(alias="bitwardenCollectionId")
    bitwarden_identity_key: str = Field(alias="bitwardenIdentityKey")
    bitwarden_identity_fields: typing.List[str] = Field(alias="bitwardenIdentityFields")
    created_at: str = Field(alias="createdAt")
    modified_at: str = Field(alias="modifiedAt")
    deleted_at: typing.Optional[str] = Field(alias="deletedAt", default=None)
    workflow_id: str = Field(alias="workflowId")

    if typing.TYPE_CHECKING:
        # Pydantic v2
        model_config: typing.ClassVar[pydantic.ConfigDict] = pydantic.ConfigDict(
            extra="allow", populate_by_name=True
        )  # type: ignore[misc]
    else:
        # Pydantic v1
        class Config:
            extra = pydantic.Extra.allow
            allow_population_by_field_name = True
            smart_union = True


class HttpBlockParametersItem_Context(UniversalBaseModel):
    parameter_type: typing_extensions.Literal["context"] = Field(alias="parameterType", default="context")
    key: str
    source: "WorkflowParameterParameterSource"
    value: typing.Any
    description: typing.Optional[str] = None

    if typing.TYPE_CHECKING:
        # Pydantic v2
        model_config: typing.ClassVar[pydantic.ConfigDict] = pydantic.ConfigDict(
            extra="allow", populate_by_name=True
        )  # type: ignore[misc]
    else:
        # Pydantic v1
        class Config:
            extra = pydantic.Extra.allow
            allow_population_by_field_name = True
            smart_union = True


class HttpBlockParametersItem_Credential(UniversalBaseModel):
    parameter_type: typing_extensions.Literal["credential"] = Field(alias="parameterType", default="credential")
    key: str
    description: typing.Optional[str] = None
    credential_parameter_id: str = Field(alias="credentialParameterId")
    credential_id: str = Field(alias="credentialId")
    created_at: str = Field(alias="createdAt")
    modified_at: str = Field(alias="modifiedAt")
    deleted_at: typing.Optional[str] = Field(alias="deletedAt", default=None)
    workflow_id: str = Field(alias="workflowId")

    if typing.TYPE_CHECKING:
        # Pydantic v2
        model_config: typing.ClassVar[pydantic.ConfigDict] = pydantic.ConfigDict(
            extra="allow", populate_by_name=True
        )  # type: ignore[misc]
    else:
        # Pydantic v1
        class Config:
            extra = pydantic.Extra.allow
            allow_population_by_field_name = True
            smart_union = True


class HttpBlockParametersItem_Output(UniversalBaseModel):
    parameter_type: typing_extensions.Literal["output"] = Field(alias="parameterType", default="output")
    key: str
    description: typing.Optional[str] = None
    output_parameter_id: str = Field(alias="outputParameterId")
    created_at: str = Field(alias="createdAt")
    modified_at: str = Field(alias="modifiedAt")
    deleted_at: typing.Optional[str] = Field(alias="deletedAt", default=None)
    workflow_id: str = Field(alias="workflowId")

    if typing.TYPE_CHECKING:
        # Pydantic v2
        model_config: typing.ClassVar[pydantic.ConfigDict] = pydantic.ConfigDict(
            extra="allow", populate_by_name=True
        )  # type: ignore[misc]
    else:
        # Pydantic v1
        class Config:
            extra = pydantic.Extra.allow
            allow_population_by_field_name = True
            smart_union = True


class HttpBlockParametersItem_Workflow(UniversalBaseModel):
    parameter_type: typing_extensions.Literal["workflow"] = Field(alias="parameterType", default="workflow")
    key: str
    description: typing.Optional[str] = None
    workflow_parameter_id: str = Field(alias="workflowParameterId")
    workflow_parameter_type: "WorkflowParameterValueType" = Field(alias="workflowParameterType")
    default_value: typing.Union[
        typing.Optional[str],
        typing.Optional[int],
        typing.Optional[float],
        typing.Optional[bool],
        typing.Optional[typing.Dict[str, typing.Any]],
        typing.Optional[typing.List[typing.Any]],
    ] = Field(alias="defaultValue")
    created_at: str = Field(alias="createdAt")
    modified_at: str = Field(alias="modifiedAt")
    deleted_at: typing.Optional[str] = Field(alias="deletedAt", default=None)
    workflow_id: str = Field(alias="workflowId")

    if typing.TYPE_CHECKING:
        # Pydantic v2
        model_config: typing.ClassVar[pydantic.ConfigDict] = pydantic.ConfigDict(
            extra="allow", populate_by_name=True
        )  # type: ignore[misc]
    else:
        # Pydantic v1
        class Config:
            extra = pydantic.Extra.allow
            allow_population_by_field_name = True
            smart_union = True


"""
from typing import *
from .workflow_parameter_parameter_source import WorkflowParameterParameterSource
from .workflow_parameter_value_type import WorkflowParameterValueType
from ..core.universal_base_model import UniversalBaseModel
from ..core.universal_base_model import pydantic
import typing
import pydantic.v1 as pydantic

This type is a discriminated union of all the possible parameter types.
"""
HttpBlockParametersItem = Union[
    HttpBlockParametersItem_AwsSecret,
    HttpBlockParametersItem_BitwardenCreditCardData,
    HttpBlockParametersItem_BitwardenLoginCredential,
    HttpBlockParametersItem_BitwardenSensitiveInformation,
    HttpBlockParametersItem_Context,
    HttpBlockParametersItem_Credential,
    HttpBlockParametersItem_Output,
    HttpBlockParametersItem_Workflow,
]


if typing.TYPE_CHECKING:
    import pydantic.v1 as pydantic
else:
    import pydantic

from .workflow_parameter_parameter_source import WorkflowParameterParameterSource  # noqa: E402
from .workflow_parameter_value_type import WorkflowParameterValueType  # noqa: E402