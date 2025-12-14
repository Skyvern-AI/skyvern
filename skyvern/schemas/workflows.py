import abc
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from skyvern.config import settings
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter, ParameterType, WorkflowParameterType
from skyvern.schemas.runs import GeoTarget, ProxyLocation, RunEngine


class WorkflowStatus(StrEnum):
    published = "published"
    draft = "draft"
    auto_generated = "auto_generated"
    importing = "importing"
    import_failed = "import_failed"


class BlockType(StrEnum):
    TASK = "task"
    TaskV2 = "task_v2"
    FOR_LOOP = "for_loop"
    CONDITIONAL = "conditional"
    CODE = "code"
    TEXT_PROMPT = "text_prompt"
    DOWNLOAD_TO_S3 = "download_to_s3"
    UPLOAD_TO_S3 = "upload_to_s3"
    FILE_UPLOAD = "file_upload"
    SEND_EMAIL = "send_email"
    FILE_URL_PARSER = "file_url_parser"
    VALIDATION = "validation"
    ACTION = "action"
    NAVIGATION = "navigation"
    EXTRACTION = "extraction"
    LOGIN = "login"
    WAIT = "wait"
    FILE_DOWNLOAD = "file_download"
    GOTO_URL = "goto_url"
    PDF_PARSER = "pdf_parser"
    HTTP_REQUEST = "http_request"
    HUMAN_INTERACTION = "human_interaction"


class BlockStatus(StrEnum):
    running = "running"
    completed = "completed"
    failed = "failed"
    terminated = "terminated"
    canceled = "canceled"
    timed_out = "timed_out"


@dataclass(frozen=True)
class BlockResult:
    success: bool
    output_parameter: OutputParameter
    output_parameter_value: dict[str, Any] | list | str | None = None
    status: BlockStatus | None = None
    failure_reason: str | None = None
    workflow_run_block_id: str | None = None


class FileType(StrEnum):
    CSV = "csv"
    EXCEL = "excel"
    PDF = "pdf"


class FileStorageType(StrEnum):
    S3 = "s3"
    AZURE = "azure"


class ParameterYAML(BaseModel, abc.ABC):
    parameter_type: ParameterType
    key: str
    description: str | None = None

    @field_validator("key")
    @classmethod
    def validate_no_whitespace(cls, v: str) -> str:
        if any(char in v for char in [" ", "\t", "\n", "\r"]):
            raise ValueError("Key cannot contain whitespaces")
        return v


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


class OnePasswordCredentialParameterYAML(ParameterYAML):
    parameter_type: Literal[ParameterType.ONEPASSWORD] = ParameterType.ONEPASSWORD  # type: ignore
    vault_id: str
    item_id: str


class AzureVaultCredentialParameterYAML(ParameterYAML):
    parameter_type: Literal[ParameterType.AZURE_VAULT_CREDENTIAL] = ParameterType.AZURE_VAULT_CREDENTIAL  # type: ignore
    vault_name: str
    username_key: str
    password_key: str
    totp_secret_key: str | None = None


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
    label: str = Field(description="Author-facing identifier; must be unique per workflow.")
    next_block_label: str | None = Field(
        default=None,
        description="Optional pointer to the label of the next block. "
        "When omitted, it will default to sequential order. See [[s-4bnl]].",
    )
    continue_on_failure: bool = False
    model: dict[str, Any] | None = None
    # Only valid for blocks inside a for loop block
    # Whether to continue to the next iteration when the block fails
    next_loop_on_failure: bool = False

    @field_validator("label")
    @classmethod
    def validate_label(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("Block labels cannot be empty.")
        return value


class TaskBlockYAML(BlockYAML):
    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    # This pattern already works in block.py but since the BlockType is not defined in this file, mypy is not able
    # to infer the type of the parameter_type attribute.
    block_type: Literal[BlockType.TASK] = BlockType.TASK  # type: ignore

    url: str | None = None
    title: str = ""
    engine: RunEngine = RunEngine.skyvern_v1
    navigation_goal: str | None = None
    data_extraction_goal: str | None = None
    data_schema: dict[str, Any] | list | str | None = None
    error_code_mapping: dict[str, str] | None = None
    max_retries: int = 0
    max_steps_per_run: int | None = None
    parameter_keys: list[str] | None = None
    complete_on_download: bool = False
    download_suffix: str | None = (
        None  # DEPRECATED: This field now sets the complete filename instead of appending to a random name
    )
    totp_verification_url: str | None = None
    totp_identifier: str | None = None
    disable_cache: bool = False
    complete_criterion: str | None = None
    terminate_criterion: str | None = None
    complete_verification: bool = True
    include_action_history_in_verification: bool = False


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


class BranchCriteriaYAML(BaseModel):
    criteria_type: Literal["jinja2_template", "prompt"] = "jinja2_template"
    expression: str
    description: str | None = None


class BranchConditionYAML(BaseModel):
    criteria: BranchCriteriaYAML | None = None
    next_block_label: str | None = None
    description: str | None = None
    is_default: bool = False

    @model_validator(mode="after")
    def validate_condition(cls, condition: "BranchConditionYAML") -> "BranchConditionYAML":
        if condition.criteria is None and not condition.is_default:
            raise ValueError("Branches without criteria must be marked as default.")
        if condition.criteria is not None and condition.is_default:
            raise ValueError("Default branches may not define criteria.")
        return condition


class ConditionalBlockYAML(BlockYAML):
    block_type: Literal[BlockType.CONDITIONAL] = BlockType.CONDITIONAL  # type: ignore

    branch_conditions: list[BranchConditionYAML] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_branches(cls, block: "ConditionalBlockYAML") -> "ConditionalBlockYAML":
        if not block.branch_conditions:
            raise ValueError("Conditional blocks require at least one branch.")

        default_branches = [branch for branch in block.branch_conditions if branch.is_default]
        if len(default_branches) > 1:
            raise ValueError("Only one default branch is permitted per conditional block.")

        return block


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

    llm_key: str | None = None
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


class FileUploadBlockYAML(BlockYAML):
    block_type: Literal[BlockType.FILE_UPLOAD] = BlockType.FILE_UPLOAD  # type: ignore

    storage_type: FileStorageType = FileStorageType.S3
    s3_bucket: str | None = None
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    region_name: str | None = None
    azure_storage_account_name: str | None = None
    azure_storage_account_key: str | None = None
    azure_blob_container_name: str | None = None
    azure_folder_path: str | None = None
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
    json_schema: dict[str, Any] | None = None


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
    disable_cache: bool = False


class ActionBlockYAML(BlockYAML):
    block_type: Literal[BlockType.ACTION] = BlockType.ACTION  # type: ignore

    url: str | None = None
    title: str = ""
    engine: RunEngine = RunEngine.skyvern_v1
    navigation_goal: str | None = None
    error_code_mapping: dict[str, str] | None = None
    max_retries: int = 0
    parameter_keys: list[str] | None = None
    complete_on_download: bool = False
    download_suffix: str | None = (
        None  # DEPRECATED: This field now sets the complete filename instead of appending to a random name
    )
    totp_verification_url: str | None = None
    totp_identifier: str | None = None
    disable_cache: bool = False


class NavigationBlockYAML(BlockYAML):
    block_type: Literal[BlockType.NAVIGATION] = BlockType.NAVIGATION  # type: ignore

    navigation_goal: str
    url: str | None = None
    title: str = ""
    engine: RunEngine = RunEngine.skyvern_v1
    error_code_mapping: dict[str, str] | None = None
    max_retries: int = 0
    max_steps_per_run: int | None = None
    parameter_keys: list[str] | None = None
    complete_on_download: bool = False
    download_suffix: str | None = (
        None  # DEPRECATED: This field now sets the complete filename instead of appending to a random name
    )
    totp_verification_url: str | None = None
    totp_identifier: str | None = None
    disable_cache: bool = False
    complete_criterion: str | None = None
    terminate_criterion: str | None = None
    complete_verification: bool = True
    include_action_history_in_verification: bool = False


class ExtractionBlockYAML(BlockYAML):
    block_type: Literal[BlockType.EXTRACTION] = BlockType.EXTRACTION  # type: ignore

    data_extraction_goal: str
    url: str | None = None
    title: str = ""
    engine: RunEngine = RunEngine.skyvern_v1
    data_schema: dict[str, Any] | list | str | None = None
    max_retries: int = 0
    max_steps_per_run: int | None = None
    parameter_keys: list[str] | None = None
    disable_cache: bool = False


class LoginBlockYAML(BlockYAML):
    block_type: Literal[BlockType.LOGIN] = BlockType.LOGIN  # type: ignore

    url: str | None = None
    title: str = ""
    engine: RunEngine = RunEngine.skyvern_v1
    navigation_goal: str | None = None
    error_code_mapping: dict[str, str] | None = None
    max_retries: int = 0
    max_steps_per_run: int | None = None
    parameter_keys: list[str] | None = None
    totp_verification_url: str | None = None
    totp_identifier: str | None = None
    disable_cache: bool = False
    complete_criterion: str | None = None
    terminate_criterion: str | None = None
    complete_verification: bool = True


class WaitBlockYAML(BlockYAML):
    block_type: Literal[BlockType.WAIT] = BlockType.WAIT  # type: ignore
    wait_sec: int = 0


class HumanInteractionBlockYAML(BlockYAML):
    block_type: Literal[BlockType.HUMAN_INTERACTION] = BlockType.HUMAN_INTERACTION  # type: ignore

    instructions: str = "Please review and approve or reject to continue the workflow."
    positive_descriptor: str = "Approve"
    negative_descriptor: str = "Reject"
    timeout_seconds: int

    sender: str
    recipients: list[str]
    subject: str
    body: str


class FileDownloadBlockYAML(BlockYAML):
    block_type: Literal[BlockType.FILE_DOWNLOAD] = BlockType.FILE_DOWNLOAD  # type: ignore

    navigation_goal: str
    url: str | None = None
    title: str = ""
    engine: RunEngine = RunEngine.skyvern_v1
    error_code_mapping: dict[str, str] | None = None
    max_retries: int = 0
    max_steps_per_run: int | None = None
    parameter_keys: list[str] | None = None
    download_suffix: str | None = (
        None  # DEPRECATED: This field now sets the complete filename instead of appending to a random name
    )
    totp_verification_url: str | None = None
    totp_identifier: str | None = None
    disable_cache: bool = False
    download_timeout: float | None = None


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
    disable_cache: bool = False


class HttpRequestBlockYAML(BlockYAML):
    block_type: Literal[BlockType.HTTP_REQUEST] = BlockType.HTTP_REQUEST  # type: ignore

    # Individual HTTP parameters
    method: str = "GET"
    url: str | None = None
    headers: dict[str, str] | None = None
    body: dict[str, Any] | None = None  # Changed to consistently be dict only
    files: dict[str, str] | None = None  # Dictionary mapping field names to file paths/URLs for multipart file uploads
    timeout: int = 30
    follow_redirects: bool = True

    # Parameter keys for templating
    parameter_keys: list[str] | None = None


PARAMETER_YAML_SUBCLASSES = (
    AWSSecretParameterYAML
    | BitwardenLoginCredentialParameterYAML
    | BitwardenSensitiveInformationParameterYAML
    | BitwardenCreditCardDataParameterYAML
    | OnePasswordCredentialParameterYAML
    | AzureVaultCredentialParameterYAML
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
    | FileUploadBlockYAML
    | SendEmailBlockYAML
    | FileParserBlockYAML
    | ValidationBlockYAML
    | ActionBlockYAML
    | NavigationBlockYAML
    | ExtractionBlockYAML
    | LoginBlockYAML
    | WaitBlockYAML
    | HumanInteractionBlockYAML
    | FileDownloadBlockYAML
    | UrlBlockYAML
    | PDFParserBlockYAML
    | TaskV2BlockYAML
    | HttpRequestBlockYAML
    | ConditionalBlockYAML
)
BLOCK_YAML_TYPES = Annotated[BLOCK_YAML_SUBCLASSES, Field(discriminator="block_type")]


class WorkflowDefinitionYAML(BaseModel):
    version: int = 1
    parameters: list[PARAMETER_YAML_TYPES]
    blocks: list[BLOCK_YAML_TYPES]

    @model_validator(mode="after")
    def validate_unique_block_labels(cls, workflow: "WorkflowDefinitionYAML") -> "WorkflowDefinitionYAML":
        labels = [block.label for block in workflow.blocks]
        duplicates = [label for label in labels if labels.count(label) > 1]

        if duplicates:
            unique_duplicates = sorted(set(duplicates))
            raise ValueError(
                f"Block labels must be unique within a workflow. "
                f"Found duplicate label(s): {', '.join(unique_duplicates)}"
            )

        return workflow


class WorkflowCreateYAMLRequest(BaseModel):
    title: str
    description: str | None = None
    proxy_location: ProxyLocation | GeoTarget | dict | None = None
    webhook_callback_url: str | None = None
    totp_verification_url: str | None = None
    totp_identifier: str | None = None
    persist_browser_session: bool = False
    model: dict[str, Any] | None = None
    workflow_definition: WorkflowDefinitionYAML
    is_saved_task: bool = False
    max_screenshot_scrolls: int | None = None
    extra_http_headers: dict[str, str] | None = None
    status: WorkflowStatus = WorkflowStatus.published
    run_with: str | None = None
    ai_fallback: bool = False
    cache_key: str | None = "default"
    run_sequentially: bool = False
    sequential_key: str | None = None
    folder_id: str | None = None


class WorkflowRequest(BaseModel):
    json_definition: WorkflowCreateYAMLRequest | None = Field(
        default=None,
        description="Workflow definition in JSON format",
    )
    yaml_definition: str | None = Field(
        default=None,
        description="Workflow definition in YAML format",
    )
