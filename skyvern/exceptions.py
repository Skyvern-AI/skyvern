from pathlib import Path

from fastapi import status


class SkyvernException(Exception):
    def __init__(self, message: str | None = None):
        self.message = message
        super().__init__(message)


class SkyvernClientException(SkyvernException):
    def __init__(self, message: str | None = None, status_code: int | None = None):
        self.status_code = status_code
        super().__init__(message)


class SkyvernHTTPException(SkyvernException):
    def __init__(self, message: str | None = None, status_code: int = status.HTTP_400_BAD_REQUEST):
        self.status_code = status_code
        super().__init__(message)


class DisabledBlockExecutionError(SkyvernHTTPException):
    def __init__(self, message: str | None = None):
        super().__init__(message, status_code=status.HTTP_400_BAD_REQUEST)


class InvalidOpenAIResponseFormat(SkyvernException):
    def __init__(self, message: str | None = None):
        super().__init__(f"Invalid response format: {message}")


class FailedToSendWebhook(SkyvernException):
    def __init__(
        self,
        task_id: str | None = None,
        workflow_run_id: str | None = None,
        workflow_id: str | None = None,
        task_v2_id: str | None = None,
    ):
        workflow_run_str = f"workflow_run_id={workflow_run_id}" if workflow_run_id else ""
        workflow_str = f"workflow_id={workflow_id}" if workflow_id else ""
        task_str = f"task_id={task_id}" if task_id else ""
        task_v2_str = f"task_v2_id={task_v2_id}" if task_v2_id else ""
        super().__init__(f"Failed to send webhook. {workflow_run_str} {workflow_str} {task_str} {task_v2_str}")


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
    def __init__(self, selector: str | None = None, element_id: str | None = None):
        super().__init__(
            f"Found no elements. Might be due to previous actions which removed this element."
            f" selector={selector} element_id={element_id}",
        )


class MultipleElementsFound(SkyvernException):
    def __init__(self, num: int, selector: str | None = None, element_id: str | None = None):
        super().__init__(
            f"Found {num} elements. Expected 1. num_elements={num} selector={selector} element_id={element_id}",
        )


class MissingFileUrl(SkyvernException):
    def __init__(self) -> None:
        super().__init__("File url is missing.")


class ImaginaryFileUrl(SkyvernException):
    def __init__(self, file_url: str) -> None:
        super().__init__(f"File url {file_url} is imaginary.")


class MissingBrowserState(SkyvernException):
    def __init__(self, task_id: str | None = None, workflow_run_id: str | None = None) -> None:
        task_str = f"task_id={task_id}" if task_id else ""
        workflow_run_str = f"workflow_run_id={workflow_run_id}" if workflow_run_id else ""
        super().__init__(f"Browser state for {task_str} {workflow_run_str} is missing.")


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


class WorkflowRunNotFound(SkyvernHTTPException):
    def __init__(self, workflow_run_id: str) -> None:
        super().__init__(f"WorkflowRun {workflow_run_id} not found", status_code=status.HTTP_404_NOT_FOUND)


class MissingValueForParameter(SkyvernHTTPException):
    def __init__(self, parameter_key: str, workflow_id: str, workflow_run_id: str) -> None:
        super().__init__(
            f"Missing value for parameter {parameter_key} in workflow run {workflow_run_id} of workflow {workflow_id}",
            status_code=status.HTTP_400_BAD_REQUEST,
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


class DownloadFileMaxWaitingTime(SkyvernException):
    def __init__(self, downloading_files: list[Path]) -> None:
        self.downloading_files = downloading_files
        super().__init__(f"Long-time downloading files [{downloading_files}].")


class NoFileDownloadTriggered(SkyvernException):
    def __init__(self, element_id: str) -> None:
        super().__init__(f"Clicking on element doesn't trigger the file download. element_id={element_id}")


class BitwardenSecretError(SkyvernException):
    def __init__(self, message: str) -> None:
        super().__init__(f"Bitwarden secret error: {message}")


class BitwardenBaseError(SkyvernException):
    def __init__(self, message: str) -> None:
        super().__init__(f"Bitwarden error: {message}")


class BitwardenLoginError(BitwardenBaseError):
    def __init__(self, message: str) -> None:
        super().__init__(f"Error logging in to Bitwarden: {message}")


class BitwardenUnlockError(BitwardenBaseError):
    def __init__(self, message: str) -> None:
        super().__init__(f"Error unlocking Bitwarden: {message}")


class BitwardenCreateCollectionError(BitwardenBaseError):
    def __init__(self, message: str) -> None:
        super().__init__(f"Error creating collection in Bitwarden: {message}")


class BitwardenCreateLoginItemError(BitwardenBaseError):
    def __init__(self, message: str) -> None:
        super().__init__(f"Error creating login item in Bitwarden: {message}")


class BitwardenCreateCreditCardItemError(BitwardenBaseError):
    def __init__(self, message: str) -> None:
        super().__init__(f"Error creating credit card item in Bitwarden: {message}")


class BitwardenCreateFolderError(BitwardenBaseError):
    def __init__(self, message: str) -> None:
        super().__init__(f"Error creating folder in Bitwarden: {message}")


class BitwardenGetItemError(BitwardenBaseError):
    def __init__(self, message: str) -> None:
        super().__init__(f"Error getting item in Bitwarden: {message}")


class BitwardenListItemsError(BitwardenBaseError):
    def __init__(self, message: str) -> None:
        super().__init__(f"Error listing items in Bitwarden: {message}")


class BitwardenTOTPError(BitwardenBaseError):
    def __init__(self, message: str) -> None:
        super().__init__(f"Error generating TOTP in Bitwarden: {message}")


class BitwardenLogoutError(BitwardenBaseError):
    def __init__(self, message: str) -> None:
        super().__init__(f"Error logging out of Bitwarden: {message}")


class BitwardenSyncError(BitwardenBaseError):
    def __init__(self, message: str) -> None:
        super().__init__(f"Error syncing Bitwarden: {message}")


class BitwardenAccessDeniedError(BitwardenBaseError):
    def __init__(self) -> None:
        super().__init__(
            "Current organization does not have access to the specified Bitwarden collection. "
            "Contact Skyvern support to enable access. This is a security layer on top of Bitwarden, "
            "Skyvern team needs to let your Skyvern account access the Bitwarden collection."
        )


class CredentialParameterParsingError(SkyvernException):
    def __init__(self, message: str) -> None:
        super().__init__(f"Error parsing credential parameter: {message}")


class CredentialParameterNotFoundError(SkyvernException):
    def __init__(self, credential_parameter_id: str) -> None:
        super().__init__(f"Could not find credential parameter: {credential_parameter_id}")


class UnknownElementTreeFormat(SkyvernException):
    def __init__(self, fmt: str) -> None:
        super().__init__(f"Unknown element tree format {fmt}")


class TerminationError(SkyvernException):
    def __init__(self, reason: str, step_id: str | None = None, task_id: str | None = None) -> None:
        super().__init__(f"Termination error. Reason: {reason}")


class StepTerminationError(TerminationError):
    def __init__(self, reason: str, step_id: str | None = None, task_id: str | None = None) -> None:
        super().__init__(f"Step {step_id} cannot be executed and task is failed. Reason: {reason}")


class TaskTerminationError(TerminationError):
    def __init__(self, reason: str, step_id: str | None = None, task_id: str | None = None) -> None:
        super().__init__(f"Task {task_id} failed. Reason: {reason}")


class BlockTerminationError(SkyvernException):
    def __init__(self, workflow_run_block_id: str, workflow_run_id: str, reason: str) -> None:
        super().__init__(
            f"Block {workflow_run_block_id} cannot be executed and workflow run {workflow_run_id} is failed. Reason: {reason}"
        )


class StepUnableToExecuteError(SkyvernException):
    def __init__(self, step_id: str, reason: str) -> None:
        super().__init__(f"Step {step_id} cannot be executed and task execution is stopped. Reason: {reason}")


class SVGConversionFailed(SkyvernException):
    def __init__(self, svg_html: str) -> None:
        super().__init__(f"Failed to convert SVG after max retries. svg_html={svg_html}")


class UnsupportedActionType(SkyvernException):
    def __init__(self, action_type: str):
        super().__init__(f"Unsupport action type: {action_type}")


class InvalidElementForTextInput(SkyvernException):
    def __init__(self, element_id: str, tag_name: str):
        super().__init__(f"The {tag_name} element with id={element_id} doesn't support text input.")


class ElementIsNotLabel(SkyvernException):
    def __init__(self, tag_name: str):
        super().__init__(f"<{tag_name}> element is not <label>")


class NoneFrameError(SkyvernException):
    def __init__(self, frame_id: str):
        super().__init__(f"frame content is none. frame_id={frame_id}")


class MissingElementDict(SkyvernException):
    def __init__(self, element_id: str) -> None:
        super().__init__(f"Invalid element id. element_id={element_id}")


class MissingElementInIframe(SkyvernException):
    def __init__(self, element_id: str) -> None:
        super().__init__(f"Found no iframe includes the element. element_id={element_id}")


class MissingElementInCSSMap(SkyvernException):
    def __init__(self, element_id: str) -> None:
        super().__init__(f"Found no css selector in the CSS map for the element. element_id={element_id}")


class InputActionOnSelect2Dropdown(SkyvernException):
    def __init__(self, element_id: str):
        super().__init__(
            f"Input action on a select element, please try to use select action on this element. element_id={element_id}"
        )


class FailToClick(SkyvernException):
    def __init__(self, element_id: str, msg: str, anchor: str = "self"):
        super().__init__(f"Failed to click({anchor}). element_id={element_id}, error_msg={msg}")


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


class TaskAlreadyTimeout(SkyvernException):
    def __init__(self, task_id: str):
        super().__init__(f"Task {task_id} is timed out")


class InvalidTaskStatusTransition(SkyvernHTTPException):
    def __init__(self, old_status: str, new_status: str, task_id: str):
        super().__init__(f"Invalid task status transition from {old_status} to {new_status} for {task_id}")


class ErrFoundSelectableElement(SkyvernException):
    def __init__(self, element_id: str, err: Exception):
        super().__init__(
            f"error when selecting elements in the children list. element_id={element_id}, error={repr(err)}"
        )


class NoSelectableElementFound(SkyvernException):
    def __init__(self, element_id: str):
        super().__init__(f"No selectable elements found in the children list. element_id={element_id}")


class HttpException(SkyvernException):
    def __init__(self, status_code: int, url: str, msg: str | None = None) -> None:
        super().__init__(f"HTTP Exception, status_code={status_code}, url={url}" + (f", msg={msg}" if msg else ""))


class WrongElementToUploadFile(SkyvernException):
    def __init__(self, element_id: str):
        super().__init__(
            f"No file chooser dialog opens, so file can't be uploaded through element {element_id}. Please try to upload again with another element."
        )


class FailedToFetchSecret(SkyvernException):
    def __init__(self) -> None:
        super().__init__("Failed to get the actual value of the secret parameter")


class NoIncrementalElementFoundForCustomSelection(SkyvernException):
    def __init__(self, element_id: str) -> None:
        super().__init__(
            f"No incremental element found, try it again later or try another element. element_id={element_id}"
        )


class NoAvailableOptionFoundForCustomSelection(SkyvernException):
    def __init__(self, reason: str | None) -> None:
        super().__init__(f"No available option to select. reason: {reason}.")


class NoElementMatchedForTargetOption(SkyvernException):
    def __init__(self, target: str, reason: str | None) -> None:
        super().__init__(
            f"No element matches for the target value, try another value. reason: {reason}.  target_value='{target}'."
        )


class NoElementBoudingBox(SkyvernException):
    def __init__(self, element_id: str) -> None:
        super().__init__(f"Element does not have a bounding box. element_id={element_id}")


class NoIncrementalElementFoundForAutoCompletion(SkyvernException):
    def __init__(self, element_id: str, text: str) -> None:
        super().__init__(f"No auto completion shown up after fill in [{text}]. element_id={element_id}")


class NoSuitableAutoCompleteOption(SkyvernException):
    def __init__(self, reasoning: str | None, target_value: str) -> None:
        super().__init__(
            f"No suitable auto complete option to choose. target_value={target_value}, reasoning={reasoning}"
        )


class NoAutoCompleteOptionMeetCondition(SkyvernException):
    def __init__(
        self, reasoning: str | None, required_relevance: float, target_value: str, closest_relevance: float
    ) -> None:
        super().__init__(
            f"No auto complete option meet the condition(relevance_float>{required_relevance}). reasoning={reasoning}, target_value={target_value}, closest_relevance={closest_relevance}"
        )


class ErrEmptyTweakValue(SkyvernException):
    def __init__(self, reasoning: str | None, current_value: str) -> None:
        super().__init__(
            f"Empty tweaked value for the current value. reasoning={reasoning}, current_value={current_value}"
        )


class FailToFindAutocompleteOption(SkyvernException):
    def __init__(self, current_value: str) -> None:
        super().__init__(
            f"Can't find a suitable auto completion for the current value, maybe retry with another reasonable value. current_value={current_value}"
        )


class IllegitComplete(SkyvernException):
    def __init__(self, data: dict | None = None) -> None:
        data_str = f", data={data}" if data else ""
        super().__init__(f"Illegit complete{data_str}")


class CachedActionPlanError(SkyvernException):
    def __init__(self, message: str) -> None:
        super().__init__(message)


class InvalidUrl(SkyvernHTTPException):
    def __init__(self, url: str) -> None:
        super().__init__(f"Invalid URL: {url}. Skyvern supports HTTP and HTTPS urls with max 2083 character length.")


class BlockedHost(SkyvernHTTPException):
    def __init__(self, host: str) -> None:
        super().__init__(
            f"The host in your url is blocked: {host}",
            status_code=status.HTTP_400_BAD_REQUEST,
        )


class InvalidWorkflowParameter(SkyvernHTTPException):
    def __init__(self, expected_parameter_type: str, value: str, workflow_permanent_id: str | None = None) -> None:
        message = f"Invalid workflow parameter. Expected parameter type: {expected_parameter_type}. Value: {value}."
        if workflow_permanent_id:
            message += f" Workflow permanent id: {workflow_permanent_id}"
        super().__init__(
            message,
            status_code=status.HTTP_400_BAD_REQUEST,
        )


class InteractWithDisabledElement(SkyvernException):
    def __init__(self, element_id: str):
        super().__init__(
            f"The element(id={element_id}) now is disabled, try to interact with it later when it's enabled."
        )


class FailedToParseActionInstruction(SkyvernException):
    def __init__(self, reason: str | None, error_type: str | None):
        super().__init__(
            f"Failed to parse the action instruction as '{reason}({error_type})'",
        )


class UnsupportedTaskType(SkyvernException):
    def __init__(self, task_type: str):
        super().__init__(f"Not supported task type [{task_type}]")


class InteractWithDropdownContainer(SkyvernException):
    def __init__(self, element_id: str):
        super().__init__(
            f"Select on the dropdown container instead of the option, try again with another element. element_id={element_id}"
        )


class UrlGenerationFailure(SkyvernHTTPException):
    def __init__(self) -> None:
        super().__init__("Failed to generate the url for the prompt")


class TaskV2NotFound(SkyvernHTTPException):
    def __init__(self, task_v2_id: str) -> None:
        super().__init__(f"Task v2 {task_v2_id} not found")


class NoTOTPVerificationCodeFound(SkyvernHTTPException):
    def __init__(
        self,
        task_id: str | None = None,
        workflow_run_id: str | None = None,
        totp_verification_url: str | None = None,
        totp_identifier: str | None = None,
    ) -> None:
        msg = "No TOTP verification code found."
        if task_id:
            msg += f" task_id={task_id}"
        if workflow_run_id:
            msg += f" workflow_run_id={workflow_run_id}"
        if totp_verification_url:
            msg += f" totp_verification_url={totp_verification_url}"
        if totp_identifier:
            msg += f" totp_identifier={totp_identifier}"
        super().__init__(msg)


class SkyvernContextWindowExceededError(SkyvernException):
    def __init__(self) -> None:
        message = "Context window exceeded. Please contact support@skyvern.com for help."
        super().__init__(message)
