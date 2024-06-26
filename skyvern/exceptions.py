from fastapi import status


class SkyvernException(Exception):
    def __init__(self, message: str | None = None):
        self.message = message
        super().__init__(message)


class SkyvernHTTPException(SkyvernException):
    def __init__(self, message: str | None = None, status_code: int = status.HTTP_400_BAD_REQUEST):
        self.status_code = status_code
        super().__init__(message)


class InvalidOpenAIResponseFormat(SkyvernException):
    def __init__(self, message: str | None = None):
        super().__init__(f"Invalid response format: {message}")


class FailedToSendWebhook(SkyvernException):
    def __init__(
        self,
        task_id: str | None = None,
        workflow_run_id: str | None = None,
        workflow_id: str | None = None,
    ):
        workflow_run_str = f"workflow_run_id={workflow_run_id}" if workflow_run_id else ""
        workflow_str = f"workflow_id={workflow_id}" if workflow_id else ""
        task_str = f"task_id={task_id}" if task_id else ""
        super().__init__(f"Failed to send webhook. {workflow_run_str} {workflow_str} {task_str}")


class ProxyLocationNotSupportedError(SkyvernException):
    def __init__(self, proxy_location: str | None = None):
        super().__init__(f"Unknown proxy location: {proxy_location}")


class TaskNotFound(SkyvernHTTPException):
    def __init__(self, task_id: str | None = None):
        super().__init__(f"Task {task_id} not found", status_code=status.HTTP_404_NOT_FOUND)


class ScriptNotFound(SkyvernException):
    def __init__(self, script_name: str | None = None):
        super().__init__(f"Script {script_name} not found. Has the script been registered?")


class MissingElement(SkyvernException):
    def __init__(self, xpath: str | None = None, element_id: str | None = None):
        super().__init__(
            f"Found no elements. Might be due to previous actions which removed this element."
            f" xpath={xpath} element_id={element_id}",
        )


class MultipleElementsFound(SkyvernException):
    def __init__(self, num: int, xpath: str | None = None, element_id: str | None = None):
        super().__init__(
            f"Found {num} elements. Expected 1. num_elements={num} xpath={xpath} element_id={element_id}",
        )


class MissingFileUrl(SkyvernException):
    def __init__(self) -> None:
        super().__init__("File url is missing.")


class ImaginaryFileUrl(SkyvernException):
    def __init__(self, file_url: str) -> None:
        super().__init__(f"File url {file_url} is imaginary.")


class MissingBrowserState(SkyvernException):
    def __init__(self, task_id: str) -> None:
        super().__init__(f"Browser state for task {task_id} is missing.")


class MissingBrowserStatePage(SkyvernException):
    def __init__(self, task_id: str | None = None, workflow_run_id: str | None = None):
        task_str = f"task_id={task_id}" if task_id else ""
        workflow_run_str = f"workflow_run_id={workflow_run_id}" if workflow_run_id else ""
        super().__init__(f"Browser state page is missing. {task_str} {workflow_run_str}")


class MissingWorkflowRunBrowserState(SkyvernException):
    def __init__(self, workflow_run_id: str, task_id: str) -> None:
        super().__init__(f"Browser state for workflow run {workflow_run_id} and task {task_id} is missing.")


class CaptchaNotSolvedInTime(SkyvernException):
    def __init__(self, task_id: str, final_state: str) -> None:
        super().__init__(f"Captcha not solved in time for task {task_id}. Final state: {final_state}")


class EnablingCaptchaSolver(SkyvernException):
    def __init__(self) -> None:
        super().__init__("Enabling captcha solver. Reload the page and try again.")


class ContextParameterValueNotFound(SkyvernException):
    def __init__(self, parameter_key: str, existing_keys: list[str], workflow_run_id: str) -> None:
        super().__init__(
            f"Context parameter value not found during workflow run {workflow_run_id}. "
            f"Parameter key: {parameter_key}. Existing keys: {existing_keys}"
        )


class UnknownBlockType(SkyvernException):
    def __init__(self, block_type: str) -> None:
        super().__init__(f"Unknown block type {block_type}")


class WorkflowNotFound(SkyvernHTTPException):
    def __init__(
        self,
        workflow_id: str | None = None,
        workflow_permanent_id: str | None = None,
        version: int | None = None,
    ) -> None:
        workflow_repr = ""
        if workflow_id:
            workflow_repr = f"workflow_id={workflow_id}"
        if workflow_permanent_id:
            if version:
                workflow_repr = f"workflow_permanent_id={workflow_permanent_id}, version={version}"
            else:
                workflow_repr = f"workflow_permanent_id={workflow_permanent_id}"

        super().__init__(
            f"Workflow not found. {workflow_repr}",
            status_code=status.HTTP_404_NOT_FOUND,
        )


class WorkflowRunNotFound(SkyvernException):
    def __init__(self, workflow_run_id: str) -> None:
        super().__init__(f"WorkflowRun {workflow_run_id} not found")


class WorkflowOrganizationMismatch(SkyvernException):
    def __init__(self, workflow_id: str, organization_id: str) -> None:
        super().__init__(f"Workflow {workflow_id} does not belong to organization {organization_id}")


class MissingValueForParameter(SkyvernException):
    def __init__(self, parameter_key: str, workflow_id: str, workflow_run_id: str) -> None:
        super().__init__(
            f"Missing value for parameter {parameter_key} in workflow run {workflow_run_id} of workflow {workflow_id}"
        )


class WorkflowParameterNotFound(SkyvernHTTPException):
    def __init__(self, workflow_parameter_id: str) -> None:
        super().__init__(
            f"Workflow parameter {workflow_parameter_id} not found",
            status_code=status.HTTP_404_NOT_FOUND,
        )


class FailedToNavigateToUrl(SkyvernException):
    def __init__(self, url: str, error_message: str) -> None:
        self.url = url
        self.error_message = error_message
        super().__init__(f"Failed to navigate to url {url}. Error message: {error_message}")


class FailedToReloadPage(SkyvernException):
    def __init__(self, url: str, error_message: str) -> None:
        self.url = url
        self.error_message = error_message
        super().__init__(f"Failed to reload page url {url}. Error message: {error_message}")


class FailedToStopLoadingPage(SkyvernException):
    def __init__(self, url: str, error_message: str) -> None:
        self.url = url
        self.error_message = error_message
        super().__init__(f"Failed to stop loading page url {url}. Error message: {error_message}")


class UnexpectedTaskStatus(SkyvernException):
    def __init__(self, task_id: str, status: str) -> None:
        super().__init__(f"Unexpected task status {status} for task {task_id}")


class InvalidWorkflowTaskURLState(SkyvernException):
    def __init__(self, workflow_run_id: str) -> None:
        super().__init__(f"No Valid URL found in the first task of workflow run {workflow_run_id}")


class DisabledFeature(SkyvernException):
    def __init__(self, feature: str) -> None:
        super().__init__(f"Feature {feature} is disabled")


class UnknownBrowserType(SkyvernException):
    def __init__(self, browser_type: str) -> None:
        super().__init__(f"Unknown browser type {browser_type}")


class UnknownErrorWhileCreatingBrowserContext(SkyvernException):
    def __init__(self, browser_type: str, exception: Exception) -> None:
        super().__init__(
            f"Unknown error while creating browser context for {browser_type}. Exception type: {type(exception)} Exception message: {str(exception)}"
        )


class BrowserStateMissingPage(SkyvernException):
    def __init__(self) -> None:
        super().__init__("BrowserState is missing the main page")


class OrganizationNotFound(SkyvernHTTPException):
    def __init__(self, organization_id: str) -> None:
        super().__init__(
            f"Organization {organization_id} not found",
            status_code=status.HTTP_404_NOT_FOUND,
        )


class StepNotFound(SkyvernHTTPException):
    def __init__(self, organization_id: str, task_id: str, step_id: str | None = None) -> None:
        super().__init__(
            f"Step {step_id or 'latest'} not found. organization_id={organization_id} task_id={task_id}",
            status_code=status.HTTP_404_NOT_FOUND,
        )


class FailedToTakeScreenshot(SkyvernException):
    def __init__(self, error_message: str) -> None:
        super().__init__(f"Failed to take screenshot. Error message: {error_message}")


class EmptyScrapePage(SkyvernException):
    def __init__(self) -> None:
        super().__init__("Failed to scrape the page, returned an NONE result")


class WorkflowRunContextNotInitialized(SkyvernException):
    def __init__(self, workflow_run_id: str) -> None:
        super().__init__(f"WorkflowRunContext not initialized for workflow run {workflow_run_id}")


class DownloadFileMaxSizeExceeded(SkyvernException):
    def __init__(self, max_size: int) -> None:
        self.max_size = max_size
        super().__init__(f"Download file size exceeded the maximum allowed size of {max_size} MB.")


class BitwardenBaseError(SkyvernException):
    def __init__(self, message: str) -> None:
        super().__init__(f"Bitwarden error: {message}")


class BitwardenLoginError(BitwardenBaseError):
    def __init__(self, message: str) -> None:
        super().__init__(f"Error logging in to Bitwarden: {message}")


class BitwardenUnlockError(BitwardenBaseError):
    def __init__(self, message: str) -> None:
        super().__init__(f"Error unlocking Bitwarden: {message}")


class BitwardenListItemsError(BitwardenBaseError):
    def __init__(self, message: str) -> None:
        super().__init__(f"Error listing items in Bitwarden: {message}")


class BitwardenTOTPError(BitwardenBaseError):
    def __init__(self, message: str) -> None:
        super().__init__(f"Error generating TOTP in Bitwarden: {message}")


class BitwardenLogoutError(BitwardenBaseError):
    def __init__(self, message: str) -> None:
        super().__init__(f"Error logging out of Bitwarden: {message}")


class UnknownElementTreeFormat(SkyvernException):
    def __init__(self, fmt: str) -> None:
        super().__init__(f"Unknown element tree format {fmt}")


class StepTerminationError(SkyvernException):
    def __init__(self, step_id: str, reason: str) -> None:
        super().__init__(f"Step {step_id} cannot be executed and task is failed. Reason: {reason}")


class StepUnableToExecuteError(SkyvernException):
    def __init__(self, step_id: str, reason: str) -> None:
        super().__init__(f"Step {step_id} cannot be executed and task execution is stopped. Reason: {reason}")


class UnsupportedActionType(SkyvernException):
    def __init__(self, action_type: str):
        super().__init__(f"Unsupport action type: {action_type}")


class InvalidElementForTextInput(SkyvernException):
    def __init__(self, element_id: str, tag_name: str):
        super().__init__(f"The {tag_name} element with id={element_id} doesn't support text input.")


class ElementIsNotLabel(SkyvernException):
    def __init__(self, tag_name: str):
        super().__init__(f"<{tag_name}> element is not <label>")


class MissingElementDict(SkyvernException):
    def __init__(self, element_id: str) -> None:
        super().__init__(f"Found no element in the dict. element_id={element_id}")


class MissingElementInIframe(SkyvernException):
    def __init__(self, element_id: str) -> None:
        super().__init__(f"Found no iframe includes the element. element_id={element_id}")


class InputActionOnSelect2Dropdown(SkyvernException):
    def __init__(self, element_id: str):
        super().__init__(
            f"Input action on a select element, please try to use select action on this element. element_id={element_id}"
        )


class FailToSelectByLabel(SkyvernException):
    def __init__(self, element_id: str):
        super().__init__(f"Failed to select by label. element_id={element_id}")


class FailToSelectByIndex(SkyvernException):
    def __init__(self, element_id: str):
        super().__init__(f"Failed to select by index. element_id={element_id}")


class OptionIndexOutOfBound(SkyvernException):
    def __init__(self, element_id: str):
        super().__init__(f"Option index is out of bound. element_id={element_id}")


class FailToSelectByValue(SkyvernException):
    def __init__(self, element_id: str):
        super().__init__(f"Failed to select by value. element_id={element_id}")


class EmptySelect(SkyvernException):
    def __init__(self, element_id: str):
        super().__init__(
            f"nothing is selected, try to select again. element_id={element_id}",
        )


class TaskAlreadyCanceled(SkyvernHTTPException):
    def __init__(self, new_status: str, task_id: str):
        super().__init__(
            f"Invalid task status transition to {new_status} for {task_id} because task is already canceled"
        )


class InvalidTaskStatusTransition(SkyvernHTTPException):
    def __init__(self, old_status: str, new_status: str, task_id: str):
        super().__init__(f"Invalid task status transition from {old_status} to {new_status} for {task_id}")
