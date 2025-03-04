import abc
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from skyvern.config import settings
from skyvern.forge.sdk.schemas.tasks import ProxyLocation
from skyvern.forge.sdk.workflow.models.block import BlockType, FileType
from skyvern.forge.sdk.workflow.models.parameter import ParameterType, WorkflowParameterType
from skyvern.forge.sdk.workflow.models.workflow import WorkflowStatus


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
    url_parameter_key: str | None = None
    # bitwarden collection id to filter the login credentials from,
    # if not provided, no filtering will be done
    bitwarden_collection_id: str | None = None
    # bitwarden item id to request the login credential
    bitwarden_item_id: str | None = None


class CredentialParameterYAML(ParameterYAML):
    parameter_type: Literal[ParameterType.CREDENTIAL] = ParameterType.CREDENTIAL  # type: ignore
    credential_id: str


class BitwardenSensitiveInformationParameterYAML(ParameterYAML):
    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    # This pattern already works in block.py but since the ParameterType is not defined in this file, mypy is not able
    # to infer the type of the parameter_type attribute.
    parameter_type: Literal["bitwarden_sensitive_information"] = ParameterType.BITWARDEN_SENSITIVE_INFORMATION  # type: ignore

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


class BitwardenCreditCardDataParameterYAML(ParameterYAML):
    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    # This pattern already works in block.py but since the ParameterType is not defined in this file, mypy is not able
    # to infer the type of the parameter_type attribute.
    parameter_type: Literal[ParameterType.BITWARDEN_CREDIT_CARD_DATA] = ParameterType.BITWARDEN_CREDIT_CARD_DATA  # type: ignore
    # bitwarden cli required fields
    bitwarden_client_id_aws_secret_key: str
    bitwarden_client_secret_aws_secret_key: str
    bitwarden_master_password_aws_secret_key: str
    # bitwarden ids for the credit card item
    bitwarden_collection_id: str
    bitwarden_item_id: str


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
    title: str = ""
    navigation_goal: str | None = None
    data_extraction_goal: str | None = None
    data_schema: dict[str, Any] | list | None = None
    error_code_mapping: dict[str, str] | None = None
    max_retries: int = 0
    max_steps_per_run: int | None = None
    parameter_keys: list[str] | None = None
    complete_on_download: bool = False
    download_suffix: str | None = None
    totp_verification_url: str | None = None
    totp_identifier: str | None = None
    cache_actions: bool = False
    complete_criterion: str | None = None
    terminate_criterion: str | None = None


class ForLoopBlockYAML(BlockYAML):
    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    # This pattern already works in block.py but since the BlockType is not defined in this file, mypy is not able
    # to infer the type of the parameter_type attribute.
    block_type: Literal[BlockType.FOR_LOOP] = BlockType.FOR_LOOP  # type: ignore

    loop_blocks: list["BLOCK_YAML_SUBCLASSES"]
    loop_over_parameter_key: str = ""
    loop_variable_reference: str | None = None
    complete_if_empty: bool = False


class CodeBlockYAML(BlockYAML):
    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    # This pattern already works in block.py but since the BlockType is not defined in this file, mypy is not able
    # to infer the type of the parameter_type attribute.
    block_type: Literal[BlockType.CODE] = BlockType.CODE  # type: ignore

    code: str
    parameter_keys: list[str] | None = None


DEFAULT_TEXT_PROMPT_LLM_KEY = settings.SECONDARY_LLM_KEY or settings.LLM_KEY


class TextPromptBlockYAML(BlockYAML):
    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    # This pattern already works in block.py but since the BlockType is not defined in this file, mypy is not able
    # to infer the type of the parameter_type attribute.
    block_type: Literal[BlockType.TEXT_PROMPT] = BlockType.TEXT_PROMPT  # type: ignore

    llm_key: str = DEFAULT_TEXT_PROMPT_LLM_KEY
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


class FileParserBlockYAML(BlockYAML):
    block_type: Literal[BlockType.FILE_URL_PARSER] = BlockType.FILE_URL_PARSER  # type: ignore

    file_url: str
    file_type: FileType


class PDFParserBlockYAML(BlockYAML):
    block_type: Literal[BlockType.PDF_PARSER] = BlockType.PDF_PARSER  # type: ignore

    file_url: str
    json_schema: dict[str, Any] | None = None


class ValidationBlockYAML(BlockYAML):
    block_type: Literal[BlockType.VALIDATION] = BlockType.VALIDATION  # type: ignore

    complete_criterion: str | None = None
    terminate_criterion: str | None = None
    error_code_mapping: dict[str, str] | None = None
    parameter_keys: list[str] | None = None


class ActionBlockYAML(BlockYAML):
    block_type: Literal[BlockType.ACTION] = BlockType.ACTION  # type: ignore

    url: str | None = None
    title: str = ""
    navigation_goal: str | None = None
    error_code_mapping: dict[str, str] | None = None
    max_retries: int = 0
    parameter_keys: list[str] | None = None
    complete_on_download: bool = False
    download_suffix: str | None = None
    totp_verification_url: str | None = None
    totp_identifier: str | None = None
    cache_actions: bool = False


class NavigationBlockYAML(BlockYAML):
    block_type: Literal[BlockType.NAVIGATION] = BlockType.NAVIGATION  # type: ignore

    navigation_goal: str
    url: str | None = None
    title: str = ""
    error_code_mapping: dict[str, str] | None = None
    max_retries: int = 0
    max_steps_per_run: int | None = None
    parameter_keys: list[str] | None = None
    complete_on_download: bool = False
    download_suffix: str | None = None
    totp_verification_url: str | None = None
    totp_identifier: str | None = None
    cache_actions: bool = False
    complete_criterion: str | None = None
    terminate_criterion: str | None = None


class ExtractionBlockYAML(BlockYAML):
    block_type: Literal[BlockType.EXTRACTION] = BlockType.EXTRACTION  # type: ignore

    data_extraction_goal: str
    url: str | None = None
    title: str = ""
    data_schema: dict[str, Any] | list | None = None
    max_retries: int = 0
    max_steps_per_run: int | None = None
    parameter_keys: list[str] | None = None
    cache_actions: bool = False


class LoginBlockYAML(BlockYAML):
    block_type: Literal[BlockType.LOGIN] = BlockType.LOGIN  # type: ignore

    url: str | None = None
    title: str = ""
    navigation_goal: str | None = None
    error_code_mapping: dict[str, str] | None = None
    max_retries: int = 0
    max_steps_per_run: int | None = None
    parameter_keys: list[str] | None = None
    totp_verification_url: str | None = None
    totp_identifier: str | None = None
    cache_actions: bool = False
    complete_criterion: str | None = None
    terminate_criterion: str | None = None


class WaitBlockYAML(BlockYAML):
    block_type: Literal[BlockType.WAIT] = BlockType.WAIT  # type: ignore
    wait_sec: int = 0


class FileDownloadBlockYAML(BlockYAML):
    block_type: Literal[BlockType.FILE_DOWNLOAD] = BlockType.FILE_DOWNLOAD  # type: ignore

    navigation_goal: str
    url: str | None = None
    title: str = ""
    error_code_mapping: dict[str, str] | None = None
    max_retries: int = 0
    max_steps_per_run: int | None = None
    parameter_keys: list[str] | None = None
    download_suffix: str | None = None
    totp_verification_url: str | None = None
    totp_identifier: str | None = None
    cache_actions: bool = False


class UrlBlockYAML(BlockYAML):
    block_type: Literal[BlockType.GOTO_URL] = BlockType.GOTO_URL  # type: ignore
    url: str


class TaskV2BlockYAML(BlockYAML):
    block_type: Literal[BlockType.TaskV2] = BlockType.TaskV2  # type: ignore
    prompt: str
    url: str | None = None
    totp_verification_url: str | None = None
    totp_identifier: str | None = None
    max_iterations: int = settings.MAX_ITERATIONS_PER_TASK_V2
    max_steps: int = settings.MAX_STEPS_PER_TASK_V2


PARAMETER_YAML_SUBCLASSES = (
    AWSSecretParameterYAML
    | BitwardenLoginCredentialParameterYAML
    | BitwardenSensitiveInformationParameterYAML
    | BitwardenCreditCardDataParameterYAML
    | WorkflowParameterYAML
    | ContextParameterYAML
    | OutputParameterYAML
    | CredentialParameterYAML
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
    | FileParserBlockYAML
    | ValidationBlockYAML
    | ActionBlockYAML
    | NavigationBlockYAML
    | ExtractionBlockYAML
    | LoginBlockYAML
    | WaitBlockYAML
    | FileDownloadBlockYAML
    | UrlBlockYAML
    | PDFParserBlockYAML
    | TaskV2BlockYAML
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
    totp_verification_url: str | None = None
    totp_identifier: str | None = None
    persist_browser_session: bool = False
    workflow_definition: WorkflowDefinitionYAML
    is_saved_task: bool = False
    status: WorkflowStatus = WorkflowStatus.published
