from skyvern.exceptions import SkyvernException


class BaseWorkflowException(SkyvernException):
    pass


class WorkflowDefinitionHasDuplicateBlockLabels(BaseWorkflowException):
    def __init__(self, duplicate_labels: set[str]) -> None:
        super().__init__(
            f"WorkflowDefinition has blocks with duplicate labels. Each block needs to have a unique "
            f"label. Duplicate label(s): {','.join(duplicate_labels)}"
        )


class OutputParameterKeyCollisionError(BaseWorkflowException):
    def __init__(self, key: str, retry_count: int | None = None) -> None:
        message = f"Output parameter key {key} already exists in the context manager."
        if retry_count is not None:
            message += f" Retrying {retry_count} more times."
        elif retry_count == 0:
            message += " Max duplicate retries reached, aborting."
        super().__init__(message)


class WorkflowDefinitionHasDuplicateParameterKeys(BaseWorkflowException):
    def __init__(self, duplicate_keys: set[str]) -> None:
        super().__init__(
            f"WorkflowDefinition has parameters with duplicate keys. Each parameter needs to have a unique "
            f"key. Duplicate key(s): {','.join(duplicate_keys)}"
        )


class InvalidEmailClientConfiguration(BaseWorkflowException):
    def __init__(self, problems: list[str]) -> None:
        super().__init__(f"Email client configuration is invalid. These parameters are missing or invalid: {problems}")


class ContextParameterSourceNotDefined(BaseWorkflowException):
    def __init__(self, context_parameter_key: str, source_key: str) -> None:
        super().__init__(
            f"Source parameter key {source_key} for context parameter {context_parameter_key} does not exist."
        )
