import abc
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from skyvern.forge.sdk.schemas.tasks import ProxyLocation
from skyvern.forge.sdk.workflow.models.block import BlockType
from skyvern.forge.sdk.workflow.models.parameter import ParameterType, WorkflowParameterType


class ParameterYAML(BaseModel, abc.ABC):
    parameter_type: ParameterType
    key: str
    description: str | None = None


class AWSSecretParameterYAML(ParameterYAML):
    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    # This pattern already works in block.py but since the ParameterType is not defined in this file, mypy is not able
    # to infer the type of the parameter_type attribute.
    parameter_type: Literal[ParameterType.AWS_SECRET] = ParameterType.AWS_SECRET  # type: ignore
    aws_key: str


class BitwardenLoginCredentialParameterYAML(ParameterYAML):
    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    # This pattern already works in block.py but since the ParameterType is not defined in this file, mypy is not able
    # to infer the type of the parameter_type attribute.
    parameter_type: Literal[ParameterType.BITWARDEN_LOGIN_CREDENTIAL] = ParameterType.BITWARDEN_LOGIN_CREDENTIAL  # type: ignore

    # bitwarden cli required fields
    bitwarden_client_id_aws_secret_key: str
    bitwarden_client_secret_aws_secret_key: str
    bitwarden_master_password_aws_secret_key: str
    # parameter key for the url to request the login credentials from bitwarden
    url_parameter_key: str
    # bitwarden collection id to filter the login credentials from,
    # if not provided, no filtering will be done
    bitwarden_collection_id: str | None = None


class WorkflowParameterYAML(ParameterYAML):
    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    # This pattern already works in block.py but since the ParameterType is not defined in this file, mypy is not able
    # to infer the type of the parameter_type attribute.
    parameter_type: Literal[ParameterType.WORKFLOW] = ParameterType.WORKFLOW  # type: ignore
    workflow_parameter_type: WorkflowParameterType
    default_value: str | int | float | bool | dict | list | None = None


class ContextParameterYAML(ParameterYAML):
    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    # This pattern already works in block.py but since the ParameterType is not defined in this file, mypy is not able
    # to infer the type of the parameter_type attribute.
    parameter_type: Literal[ParameterType.CONTEXT] = ParameterType.CONTEXT  # type: ignore
    source_parameter_key: str


class OutputParameterYAML(ParameterYAML):
    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    # This pattern already works in block.py but since the ParameterType is not defined in this file, mypy is not able
    # to infer the type of the parameter_type attribute.
    parameter_type: Literal[ParameterType.OUTPUT] = ParameterType.OUTPUT  # type: ignore


class BlockYAML(BaseModel, abc.ABC):
    block_type: BlockType
    label: str
    continue_on_failure: bool = False


class TaskBlockYAML(BlockYAML):
    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    # This pattern already works in block.py but since the BlockType is not defined in this file, mypy is not able
    # to infer the type of the parameter_type attribute.
    block_type: Literal[BlockType.TASK] = BlockType.TASK  # type: ignore

    url: str | None = None
    title: str = "Untitled Task"
    navigation_goal: str | None = None
    data_extraction_goal: str | None = None
    data_schema: dict[str, Any] | None = None
    error_code_mapping: dict[str, str] | None = None
    max_retries: int = 0
    max_steps_per_run: int | None = None
    parameter_keys: list[str] | None = None
    complete_on_download: bool = False


class ForLoopBlockYAML(BlockYAML):
    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    # This pattern already works in block.py but since the BlockType is not defined in this file, mypy is not able
    # to infer the type of the parameter_type attribute.
    block_type: Literal[BlockType.FOR_LOOP] = BlockType.FOR_LOOP  # type: ignore

    loop_over_parameter_key: str
    loop_blocks: list["BLOCK_YAML_SUBCLASSES"]


class CodeBlockYAML(BlockYAML):
    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    # This pattern already works in block.py but since the BlockType is not defined in this file, mypy is not able
    # to infer the type of the parameter_type attribute.
    block_type: Literal[BlockType.CODE] = BlockType.CODE  # type: ignore

    code: str
    parameter_keys: list[str] | None = None


class TextPromptBlockYAML(BlockYAML):
    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    # This pattern already works in block.py but since the BlockType is not defined in this file, mypy is not able
    # to infer the type of the parameter_type attribute.
    block_type: Literal[BlockType.TEXT_PROMPT] = BlockType.TEXT_PROMPT  # type: ignore

    llm_key: str
    prompt: str
    parameter_keys: list[str] | None = None
    json_schema: dict[str, Any] | None = None


class DownloadToS3BlockYAML(BlockYAML):
    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    # This pattern already works in block.py but since the BlockType is not defined in this file, mypy is not able
    # to infer the type of the parameter_type attribute.
    block_type: Literal[BlockType.DOWNLOAD_TO_S3] = BlockType.DOWNLOAD_TO_S3  # type: ignore

    url: str


class UploadToS3BlockYAML(BlockYAML):
    block_type: Literal[BlockType.UPLOAD_TO_S3] = BlockType.UPLOAD_TO_S3  # type: ignore

    path: str | None = None


class SendEmailBlockYAML(BlockYAML):
    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    # This pattern already works in block.py but since the BlockType is not defined in this file, mypy is not able
    # to infer the type of the parameter_type attribute.
    block_type: Literal[BlockType.SEND_EMAIL] = BlockType.SEND_EMAIL  # type: ignore

    smtp_host_secret_parameter_key: str
    smtp_port_secret_parameter_key: str
    smtp_username_secret_parameter_key: str
    smtp_password_secret_parameter_key: str
    sender: str
    recipients: list[str]
    subject: str
    body: str
    file_attachments: list[str] | None = None


PARAMETER_YAML_SUBCLASSES = (
    AWSSecretParameterYAML
    | BitwardenLoginCredentialParameterYAML
    | WorkflowParameterYAML
    | ContextParameterYAML
    | OutputParameterYAML
)
PARAMETER_YAML_TYPES = Annotated[PARAMETER_YAML_SUBCLASSES, Field(discriminator="parameter_type")]

BLOCK_YAML_SUBCLASSES = (
    TaskBlockYAML
    | ForLoopBlockYAML
    | CodeBlockYAML
    | TextPromptBlockYAML
    | DownloadToS3BlockYAML
    | UploadToS3BlockYAML
    | SendEmailBlockYAML
)
BLOCK_YAML_TYPES = Annotated[BLOCK_YAML_SUBCLASSES, Field(discriminator="block_type")]


class WorkflowDefinitionYAML(BaseModel):
    parameters: list[PARAMETER_YAML_TYPES]
    blocks: list[BLOCK_YAML_TYPES]


class WorkflowCreateYAMLRequest(BaseModel):
    title: str
    description: str | None = None
    proxy_location: ProxyLocation | None = None
    webhook_callback_url: str | None = None
    workflow_definition: WorkflowDefinitionYAML
    is_saved_task: bool = False
