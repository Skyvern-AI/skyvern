from starlette import status

from testcharmvision.exceptions import TestcharmvisionException, TestcharmvisionHTTPException


class BaseWorkflowException(TestcharmvisionException):
    pass


class BaseWorkflowHTTPException(TestcharmvisionHTTPException):
    pass


class WorkflowDefinitionHasDuplicateBlockLabels(BaseWorkflowHTTPException):
    def __init__(self, duplicate_labels: set[str]) -> None:
        super().__init__(
            f"WorkflowDefinition has blocks with duplicate labels. Each block needs to have a unique "
            f"label. Duplicate label(s): {','.join(duplicate_labels)}",
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )


class InvalidFinallyBlockLabel(BaseWorkflowHTTPException):
    def __init__(self, finally_block_label: str, available_labels: list[str]) -> None:
        super().__init__(
            f"finally_block_label '{finally_block_label}' does not reference a valid block in the workflow. "
            f"Available block labels: {', '.join(available_labels) if available_labels else '(none)'}",
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )


class NonTerminalFinallyBlock(BaseWorkflowHTTPException):
    def __init__(self, finally_block_label: str) -> None:
        super().__init__(
            f"finally_block_label '{finally_block_label}' must be a terminal block (next_block_label must be null). "
            "Only blocks without a next_block_label can be used as finally blocks.",
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )


class FailedToCreateWorkflow(BaseWorkflowHTTPException):
    def __init__(self, error_message: str) -> None:
        super().__init__(
            f"Failed to create workflow. Error: {error_message}",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


class FailedToUpdateWorkflow(BaseWorkflowHTTPException):
    def __init__(self, workflow_permanent_id: str, error_message: str) -> None:
        super().__init__(
            f"Failed to update workflow with ID {workflow_permanent_id}. Error: {error_message}",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


class WorkflowVersionConflict(BaseWorkflowHTTPException):
    def __init__(self, workflow_permanent_id: str) -> None:
        super().__init__(
            f"Concurrent update detected for workflow {workflow_permanent_id}. Please retry.",
            status_code=status.HTTP_409_CONFLICT,
        )


class OutputParameterKeyCollisionError(BaseWorkflowHTTPException):
    def __init__(self, key: str, retry_count: int | None = None) -> None:
        message = f"Output parameter key {key} already exists in the context manager."
        if retry_count is not None:
            message += f" Retrying {retry_count} more times."
        elif retry_count == 0:
            message += " Max duplicate retries reached, aborting."
        super().__init__(
            message,
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )


class WorkflowDefinitionHasDuplicateParameterKeys(BaseWorkflowHTTPException):
    def __init__(self, duplicate_keys: set[str]) -> None:
        super().__init__(
            f"WorkflowDefinition has parameters with duplicate keys. Each parameter needs to have a unique "
            f"key. Duplicate key(s): {','.join(duplicate_keys)}",
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )


class WorkflowDefinitionHasReservedParameterKeys(BaseWorkflowHTTPException):
    def __init__(self, reserved_keys: list[str], parameter_keys: list[str]) -> None:
        super().__init__(
            f"WorkflowDefinition has parameters with reserved keys. User created parameters cannot have the following "
            f"reserved keys: {','.join(reserved_keys)}. Parameter keys: {','.join(parameter_keys)}",
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )


class InvalidWorkflowDefinition(BaseWorkflowHTTPException):
    def __init__(self, message: str) -> None:
        super().__init__(
            message,
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )


class InvalidEmailClientConfiguration(BaseWorkflowException):
    def __init__(self, problems: list[str]) -> None:
        super().__init__(f"Email client configuration is invalid. These parameters are missing or invalid: {problems}")


class NoValidEmailRecipient(BaseWorkflowException):
    def __init__(self, recipients: list[str]) -> None:
        super().__init__(f"No valid email recipient found. Recipients: {recipients}")


class ContextParameterSourceNotDefined(BaseWorkflowHTTPException):
    def __init__(self, context_parameter_key: str, source_key: str) -> None:
        super().__init__(
            f"Source parameter key {source_key} for context parameter {context_parameter_key} does not exist.",
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )


class InvalidFileType(BaseWorkflowHTTPException):
    def __init__(self, file_url: str, file_type: str, error: str) -> None:
        super().__init__(
            f"File URL {file_url} is not a valid {file_type} file. Error: {error}",
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )


class WorkflowDefinitionValidationException(BaseWorkflowHTTPException):
    """Base exception for workflow definition validation errors."""


class WorkflowParameterMissingRequiredValue(WorkflowDefinitionValidationException):
    def __init__(self, workflow_parameter_type: str, workflow_parameter_key: str, required_value: str) -> None:
        super().__init__(
            f"Missing required value for workflow parameter. Workflow parameter type: {workflow_parameter_type}. workflow_parameter_key: {workflow_parameter_key}. Required value: {required_value}",
            status_code=status.HTTP_400_BAD_REQUEST,
        )


class WorkflowDefinitionHasUndefinedParameters(WorkflowDefinitionValidationException):
    def __init__(self, undefined_parameters: dict[str, list[str]]) -> None:
        # Format: {"block_label": ["param1", "param2"]}
        error_details = []
        for block_label, params in undefined_parameters.items():
            params_str = ", ".join(f"'{p}'" for p in params)
            error_details.append(f"  - Block '{block_label}' references undefined parameter(s): {params_str}")

        error_message = (
            f"Workflow definition has blocks that reference undefined parameters:\n"
            f"{chr(10).join(error_details)}\n\n"
            f"Make sure to define all parameters in the workflow parameters list before using them in blocks."
        )
        super().__init__(
            error_message,
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )


class InvalidWaitBlockTime(TestcharmvisionException):
    def __init__(self, max_sec: int) -> None:
        super().__init__(f"Invalid wait time for wait block, it should be a number between 0 and {max_sec}.")


class FailedToFormatJinjaStyleParameter(TestcharmvisionException):
    def __init__(self, template: str, msg: str) -> None:
        super().__init__(
            f"Failed to format Jinja style parameter '{template}'. "
            f"Reason: {msg}. "
            "If your block labels or parameter keys contain characters like '/', '-', or '.', "
            "please rename them to use only letters, numbers, and underscores (e.g., 'State_Province' instead of 'State/Province')."
        )


class MissingJinjaVariables(TestcharmvisionException):
    def __init__(self, template: str, variables: set[str]) -> None:
        self.variables = variables

        super().__init__(
            f"Missing variables for '{template}'. Missing: {variables}. "
            "If your block labels or parameter keys contain characters like '/', '-', or '.', "
            "please rename them to use only letters, numbers, and underscores (e.g., 'State_Province' instead of 'State/Province')."
        )


class NoIterableValueFound(TestcharmvisionException):
    def __init__(self) -> None:
        super().__init__("No iterable value found for the loop block")


class InvalidTemplateWorkflowPermanentId(TestcharmvisionHTTPException):
    def __init__(self, workflow_permanent_id: str) -> None:
        super().__init__(
            message=f"Invalid template workflow permanent id: {workflow_permanent_id}. Please make sure the workflow is a valid template.",
            status_code=status.HTTP_400_BAD_REQUEST,
        )


class InsecureCodeDetected(TestcharmvisionException):
    def __init__(self, msg: str) -> None:
        super().__init__(
            f"Insecure code detected. Reason: {msg}",
        )


class CustomizedCodeException(TestcharmvisionException):
    def __init__(self, exception: Exception) -> None:
        super().__init__(
            f"Failed to execute code block. Reason: {exception.__class__.__name__}: {str(exception)}",
        )
