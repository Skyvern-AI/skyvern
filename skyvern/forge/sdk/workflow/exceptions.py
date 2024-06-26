from starlette import status

from skyvern.exceptions import SkyvernException, SkyvernHTTPException


class BaseWorkflowException(SkyvernException):
    pass


class BaseWorkflowHTTPException(SkyvernHTTPException):
    pass


class WorkflowDefinitionHasDuplicateBlockLabels(BaseWorkflowHTTPException):
    def __init__(self, duplicate_labels: set[str]) -> None:
        super().__init__(
            f"WorkflowDefinition has blocks with duplicate labels. Each block needs to have a unique "
            f"label. Duplicate label(s): {','.join(duplicate_labels)}",
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
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
