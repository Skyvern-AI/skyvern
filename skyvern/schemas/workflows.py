import abc
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Any, Literal

import structlog
from pydantic import BaseModel, Field, field_validator, model_validator

from skyvern.config import settings
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter, ParameterType, WorkflowParameterType
from skyvern.schemas.runs import GeoTarget, ProxyLocation, RunEngine
from skyvern.utils.strings import sanitize_identifier
from skyvern.utils.templating import replace_jinja_reference

LOG = structlog.get_logger()


def sanitize_block_label(value: str) -> str:
    """Sanitizes a block label to be a valid Python/Jinja2 identifier.

    Block labels are used to create output parameter keys (e.g., '{label}_output')
    which are then used as Jinja2 template variable names.

    Args:
        value: The raw label value to sanitize

    Returns:
        A sanitized label that is a valid Python identifier
    """
    return sanitize_identifier(value, default="block")


def sanitize_parameter_key(value: str) -> str:
    """Sanitizes a parameter key to be a valid Python/Jinja2 identifier.

    Parameter keys are used as Jinja2 template variable names.

    Args:
        value: The raw key value to sanitize

    Returns:
        A sanitized key that is a valid Python identifier
    """
    return sanitize_identifier(value, default="parameter")


def _replace_references_in_value(value: Any, old_key: str, new_key: str) -> Any:
    """Recursively replaces Jinja references in a value (string, dict, or list)."""
    if isinstance(value, str):
        return replace_jinja_reference(value, old_key, new_key)
    elif isinstance(value, dict):
        return {k: _replace_references_in_value(v, old_key, new_key) for k, v in value.items()}
    elif isinstance(value, list):
        return [_replace_references_in_value(item, old_key, new_key) for item in value]
    return value


def _replace_direct_string_in_value(value: Any, old_key: str, new_key: str) -> Any:
    """Recursively replaces exact string matches in a value (for fields like source_parameter_key)."""
    if isinstance(value, str):
        return new_key if value == old_key else value
    elif isinstance(value, dict):
        return {k: _replace_direct_string_in_value(v, old_key, new_key) for k, v in value.items()}
    elif isinstance(value, list):
        return [_replace_direct_string_in_value(item, old_key, new_key) for item in value]
    return value


def _make_unique(candidate: str, seen: set[str]) -> str:
    """Appends a numeric suffix to make candidate unique within the seen set.

    Args:
        candidate: The candidate identifier
        seen: Set of already-used identifiers (mutated -- the chosen name is added)

    Returns:
        A unique identifier, either candidate itself or candidate_N
    """
    if candidate not in seen:
        seen.add(candidate)
        return candidate
    counter = 2
    while f"{candidate}_{counter}" in seen:
        counter += 1
    unique = f"{candidate}_{counter}"
    seen.add(unique)
    return unique


def _sanitize_blocks_recursive(
    blocks: list[dict[str, Any]],
    label_mapping: dict[str, str],
    seen_labels: set[str],
) -> list[dict[str, Any]]:
    """Recursively sanitizes block labels and collects the label mapping.

    Args:
        blocks: List of block dictionaries
        label_mapping: Dictionary to store old_label -> new_label mappings (mutated)
        seen_labels: Set of already-used labels for collision avoidance (mutated)

    Returns:
        List of blocks with sanitized labels
    """
    sanitized_blocks = []
    for block in blocks:
        block = dict(block)  # Make a copy to avoid mutating the original

        # Sanitize the block's label
        if "label" in block and isinstance(block["label"], str):
            old_label = block["label"]
            new_label = sanitize_block_label(old_label)
            new_label = _make_unique(new_label, seen_labels)
            if old_label != new_label:
                label_mapping[old_label] = new_label
                block["label"] = new_label
            else:
                # Even if unchanged, track it as seen for collision avoidance
                seen_labels.add(old_label)

        # Handle nested blocks in for_loop
        if "loop_blocks" in block and isinstance(block["loop_blocks"], list):
            block["loop_blocks"] = _sanitize_blocks_recursive(block["loop_blocks"], label_mapping, seen_labels)

        sanitized_blocks.append(block)

    return sanitized_blocks


def _sanitize_parameters(
    parameters: list[dict[str, Any]],
    key_mapping: dict[str, str],
    seen_keys: set[str],
) -> list[dict[str, Any]]:
    """Sanitizes parameter keys and collects the key mapping.

    Args:
        parameters: List of parameter dictionaries
        key_mapping: Dictionary to store old_key -> new_key mappings (mutated)
        seen_keys: Set of already-used keys for collision avoidance (mutated)

    Returns:
        List of parameters with sanitized keys
    """
    sanitized_params = []
    for param in parameters:
        param = dict(param)  # Make a copy

        if "key" in param and isinstance(param["key"], str):
            old_key = param["key"]
            new_key = sanitize_parameter_key(old_key)
            new_key = _make_unique(new_key, seen_keys)
            if old_key != new_key:
                key_mapping[old_key] = new_key
                param["key"] = new_key
            else:
                # Even if unchanged, track it as seen for collision avoidance
                seen_keys.add(old_key)

        sanitized_params.append(param)

    return sanitized_params


def _update_parameter_keys_in_blocks(
    blocks: list[dict[str, Any]],
    key_mapping: dict[str, str],
) -> list[dict[str, Any]]:
    """Updates parameter_keys arrays in blocks to use new parameter key names.

    Args:
        blocks: List of block dictionaries
        key_mapping: Dictionary of old_key -> new_key mappings

    Returns:
        List of blocks with updated parameter_keys
    """
    updated_blocks = []
    for block in blocks:
        block = dict(block)

        # Update parameter_keys array if present
        if "parameter_keys" in block and isinstance(block["parameter_keys"], list):
            block["parameter_keys"] = [
                key_mapping.get(key, key) if isinstance(key, str) else key for key in block["parameter_keys"]
            ]

        # Handle nested blocks in for_loop
        if "loop_blocks" in block and isinstance(block["loop_blocks"], list):
            block["loop_blocks"] = _update_parameter_keys_in_blocks(block["loop_blocks"], key_mapping)

        updated_blocks.append(block)

    return updated_blocks


def sanitize_workflow_yaml_with_references(workflow_yaml: dict[str, Any]) -> dict[str, Any]:
    """Sanitizes block labels and parameter keys, and updates all references throughout the workflow.

    This function:
    1. Sanitizes all block labels to be valid Python identifiers
    2. Sanitizes all parameter keys to be valid Python identifiers
    3. Updates all Jinja references from {old_key} to {new_key}
    4. Updates next_block_label if it references an old label
    5. Updates finally_block_label if it references an old label
    6. Updates parameter_keys arrays in blocks
    7. Updates source_parameter_key and other direct references

    Args:
        workflow_yaml: The parsed workflow YAML dictionary

    Returns:
        The workflow YAML with sanitized identifiers and updated references
    """
    workflow_yaml = dict(workflow_yaml)  # Make a copy

    workflow_definition = workflow_yaml.get("workflow_definition")
    if not workflow_definition or not isinstance(workflow_definition, dict):
        return workflow_yaml

    workflow_definition = dict(workflow_definition)  # Make a copy
    workflow_yaml["workflow_definition"] = workflow_definition

    # Step 1: Sanitize all block labels and collect the mapping
    label_mapping: dict[str, str] = {}  # old_label -> new_label
    seen_labels: set[str] = set()
    blocks = workflow_definition.get("blocks")
    if blocks and isinstance(blocks, list):
        workflow_definition["blocks"] = _sanitize_blocks_recursive(blocks, label_mapping, seen_labels)

    # Step 2: Sanitize all parameter keys and collect the mapping
    param_key_mapping: dict[str, str] = {}  # old_key -> new_key
    seen_keys: set[str] = set()
    parameters = workflow_definition.get("parameters")
    if parameters and isinstance(parameters, list):
        workflow_definition["parameters"] = _sanitize_parameters(parameters, param_key_mapping, seen_keys)

    # If nothing was changed, return early
    if not label_mapping and not param_key_mapping:
        return workflow_yaml

    LOG.info(
        "Auto-sanitized workflow identifiers during import",
        sanitized_labels=label_mapping if label_mapping else None,
        sanitized_parameter_keys=param_key_mapping if param_key_mapping else None,
    )

    # Step 3: Update all block label references
    for old_label, new_label in label_mapping.items():
        old_output_key = f"{old_label}_output"
        new_output_key = f"{new_label}_output"

        # Update Jinja references in blocks for {label}_output pattern
        if "blocks" in workflow_definition:
            workflow_definition["blocks"] = _replace_references_in_value(
                workflow_definition["blocks"], old_output_key, new_output_key
            )
            # Also update shorthand {{ label }} references (must be done after _output to avoid partial matches)
            workflow_definition["blocks"] = _replace_references_in_value(
                workflow_definition["blocks"], old_label, new_label
            )

        # Update Jinja references in parameters for {label}_output pattern
        if "parameters" in workflow_definition:
            workflow_definition["parameters"] = _replace_references_in_value(
                workflow_definition["parameters"], old_output_key, new_output_key
            )
            # Also update shorthand {{ label }} references
            workflow_definition["parameters"] = _replace_references_in_value(
                workflow_definition["parameters"], old_label, new_label
            )
            # Also update direct string references (e.g., source_parameter_key)
            workflow_definition["parameters"] = _replace_direct_string_in_value(
                workflow_definition["parameters"], old_output_key, new_output_key
            )

    # Step 4: Update all parameter key references
    for old_key, new_key in param_key_mapping.items():
        # Update Jinja references in blocks (e.g., {{ old_key }})
        if "blocks" in workflow_definition:
            workflow_definition["blocks"] = _replace_references_in_value(
                workflow_definition["blocks"], old_key, new_key
            )

        # Update Jinja references in parameters (e.g., default values that reference other params)
        if "parameters" in workflow_definition:
            workflow_definition["parameters"] = _replace_references_in_value(
                workflow_definition["parameters"], old_key, new_key
            )
            # Also update direct string references (e.g., source_parameter_key)
            workflow_definition["parameters"] = _replace_direct_string_in_value(
                workflow_definition["parameters"], old_key, new_key
            )

    # Step 5: Update parameter_keys arrays in blocks
    if param_key_mapping and "blocks" in workflow_definition:
        workflow_definition["blocks"] = _update_parameter_keys_in_blocks(
            workflow_definition["blocks"], param_key_mapping
        )

    # Step 6: Update finally_block_label if it references an old label
    if "finally_block_label" in workflow_definition:
        finally_label = workflow_definition["finally_block_label"]
        if finally_label in label_mapping:
            workflow_definition["finally_block_label"] = label_mapping[finally_label]

    # Step 7: Update next_block_label in all blocks
    def update_next_block_label(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        updated_blocks = []
        for block in blocks:
            block = dict(block)
            if "next_block_label" in block:
                next_label = block["next_block_label"]
                if next_label in label_mapping:
                    block["next_block_label"] = label_mapping[next_label]
            if "loop_blocks" in block and isinstance(block["loop_blocks"], list):
                block["loop_blocks"] = update_next_block_label(block["loop_blocks"])
            updated_blocks.append(block)
        return updated_blocks

    if label_mapping and "blocks" in workflow_definition:
        workflow_definition["blocks"] = update_next_block_label(workflow_definition["blocks"])

    return workflow_yaml


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
    PRINT_PAGE = "print_page"
    WORKFLOW_TRIGGER = "workflow_trigger"


class BlockStatus(StrEnum):
    running = "running"
    completed = "completed"
    failed = "failed"
    terminated = "terminated"
    canceled = "canceled"
    timed_out = "timed_out"
    skipped = "skipped"


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
    IMAGE = "image"
    DOCX = "docx"


class PDFFormat(StrEnum):
    A4 = "A4"
    LETTER = "Letter"
    LEGAL = "Legal"
    TABLOID = "Tabloid"


class FileStorageType(StrEnum):
    S3 = "s3"
    AZURE = "azure"


class ParameterYAML(BaseModel, abc.ABC):
    parameter_type: ParameterType
    key: str
    description: str | None = None

    @field_validator("key")
    @classmethod
    def validate_key_is_valid_identifier(cls, v: str) -> str:
        """Validate that parameter key is a valid Jinja2/Python identifier.

        Parameter keys are used as Jinja2 template variable names. Jinja2 variable names
        must be valid Python identifiers (letters, digits, underscores; cannot start with digit).

        Characters like '/', '-', '.', etc. are NOT allowed because they are interpreted as
        operators in Jinja2 templates, causing parsing errors like "'State_' is undefined"
        when using a key like "State_/_Province".
        """
        if any(char in v for char in [" ", "\t", "\n", "\r"]):
            raise ValueError("Key cannot contain whitespace characters")

        if not v.isidentifier():
            raise ValueError(
                f"Key '{v}' is not a valid parameter name. "
                "Parameter keys must be valid Python identifiers "
                "(only letters, digits, and underscores; cannot start with a digit). "
                "Characters like '/', '-', '.', etc. are not allowed because they conflict with Jinja2 template syntax."
            )
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
        """Validate that block label is a valid Python identifier.

        Block labels are used to create output parameter keys (e.g., '{label}_output')
        which are then used as Jinja2 template variable names. Therefore, block labels
        must be valid Python identifiers.
        """
        if not value or not value.strip():
            raise ValueError("Block labels cannot be empty.")
        if not value.isidentifier():
            raise ValueError(
                f"Block label '{value}' is not a valid label. "
                "Block labels must be valid Python identifiers "
                "(only letters, digits, and underscores; cannot start with a digit). "
                "Characters like '/', '-', '.', etc. are not allowed because they conflict with Jinja2 template syntax."
            )
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
    data_schema: dict[str, Any] | str | None = None


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
    download_filename: str | None = None
    save_response_as_file: bool = False

    # Parameter keys for templating
    parameter_keys: list[str] | None = None


class PrintPageBlockYAML(BlockYAML):
    block_type: Literal[BlockType.PRINT_PAGE] = BlockType.PRINT_PAGE  # type: ignore
    include_timestamp: bool = True
    custom_filename: str | None = None
    format: PDFFormat = PDFFormat.A4
    landscape: bool = False
    print_background: bool = True
    parameter_keys: list[str] | None = None


class WorkflowTriggerBlockYAML(BlockYAML):
    block_type: Literal[BlockType.WORKFLOW_TRIGGER] = BlockType.WORKFLOW_TRIGGER  # type: ignore

    # The permanent ID of the target workflow to trigger
    workflow_permanent_id: str
    # Parameters/payload to pass to the triggered workflow (Jinja2 templates supported in values)
    payload: dict[str, Any] | None = None
    # Whether to wait for the triggered workflow to complete before continuing
    wait_for_completion: bool = True
    # Optional browser session ID for the triggered workflow
    browser_session_id: str | None = None
    # When True, the child workflow inherits the parent's browser session
    use_parent_browser_session: bool = False
    # Parameter keys for template interpolation
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
    | PrintPageBlockYAML
    | WorkflowTriggerBlockYAML
)
BLOCK_YAML_TYPES = Annotated[BLOCK_YAML_SUBCLASSES, Field(discriminator="block_type")]


class WorkflowDefinitionYAML(BaseModel):
    version: int = 1
    parameters: list[PARAMETER_YAML_TYPES]
    blocks: list[BLOCK_YAML_TYPES]
    finally_block_label: str | None = None

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

        if workflow.finally_block_label and workflow.finally_block_label not in labels:
            raise ValueError(
                f"finally_block_label '{workflow.finally_block_label}' does not reference a valid block. "
                f"Available labels: {', '.join(labels) if labels else '(none)'}"
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
    adaptive_caching: bool = False
    generate_script_on_terminal: bool = False
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
