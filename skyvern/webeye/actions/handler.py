import asyncio
import json
import os
import urllib.parse
import uuid
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, List

import structlog
from deprecation import deprecated
from playwright.async_api import FileChooser, Locator, Page, TimeoutError

from skyvern.constants import REPO_ROOT_DIR, VERIFICATION_CODE_PLACEHOLDER, VERIFICATION_CODE_POLLING_TIMEOUT_MINS
from skyvern.exceptions import (
    EmptySelect,
    ErrFoundSelectableElement,
    FailedToFetchSecret,
    FailToClick,
    FailToSelectByIndex,
    FailToSelectByLabel,
    FailToSelectByValue,
    ImaginaryFileUrl,
    InputActionOnSelect2Dropdown,
    InvalidElementForTextInput,
    MissingElement,
    MissingFileUrl,
    MultipleElementsFound,
    NoSelectableElementFound,
    OptionIndexOutOfBound,
    WrongElementToUploadFile,
)
from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.api.files import (
    download_file,
    get_number_of_files_in_directory,
    get_path_for_workflow_download_directory,
)
from skyvern.forge.sdk.core.aiohttp_helper import aiohttp_post
from skyvern.forge.sdk.core.security import generate_skyvern_signature
from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
from skyvern.forge.sdk.models import Step
from skyvern.forge.sdk.schemas.tasks import Task
from skyvern.forge.sdk.services.bitwarden import BitwardenConstants
from skyvern.forge.sdk.settings_manager import SettingsManager
from skyvern.webeye.actions import actions
from skyvern.webeye.actions.actions import (
    Action,
    ActionType,
    CheckboxAction,
    ClickAction,
    ScrapeResult,
    SelectOptionAction,
    UploadFileAction,
    WebAction,
)
from skyvern.webeye.actions.responses import ActionFailure, ActionResult, ActionSuccess
from skyvern.webeye.browser_factory import BrowserState
from skyvern.webeye.scraper.scraper import ElementTreeFormat, ScrapedPage
from skyvern.webeye.utils.dom import AbstractSelectDropdown, DomUtil, SkyvernElement

LOG = structlog.get_logger()
COMMON_INPUT_TAGS = {"input", "textarea", "select"}


class ActionHandler:
    _handled_action_types: dict[
        ActionType,
        Callable[[Action, Page, ScrapedPage, Task, Step], Awaitable[list[ActionResult]]],
    ] = {}

    _setup_action_types: dict[
        ActionType,
        Callable[[Action, Page, ScrapedPage, Task, Step], Awaitable[list[ActionResult]]],
    ] = {}

    _teardown_action_types: dict[
        ActionType,
        Callable[[Action, Page, ScrapedPage, Task, Step], Awaitable[list[ActionResult]]],
    ] = {}

    @classmethod
    def register_action_type(
        cls,
        action_type: ActionType,
        handler: Callable[[Action, Page, ScrapedPage, Task, Step], Awaitable[list[ActionResult]]],
    ) -> None:
        cls._handled_action_types[action_type] = handler

    @classmethod
    def register_setup_for_action_type(
        cls,
        action_type: ActionType,
        handler: Callable[[Action, Page, ScrapedPage, Task, Step], Awaitable[list[ActionResult]]],
    ) -> None:
        cls._setup_action_types[action_type] = handler

    @classmethod
    def register_teardown_for_action_type(
        cls,
        action_type: ActionType,
        handler: Callable[[Action, Page, ScrapedPage, Task, Step], Awaitable[list[ActionResult]]],
    ) -> None:
        cls._teardown_action_types[action_type] = handler

    @staticmethod
    async def handle_action(
        scraped_page: ScrapedPage,
        task: Task,
        step: Step,
        browser_state: BrowserState,
        action: Action,
    ) -> list[ActionResult]:
        LOG.info("Handling action", action=action)
        page = await browser_state.get_or_create_page()
        try:
            if action.action_type in ActionHandler._handled_action_types:
                actions_result: list[ActionResult] = []

                if invalid_web_action_check := check_for_invalid_web_action(action, page, scraped_page, task, step):
                    return invalid_web_action_check

                # do setup before action handler
                if setup := ActionHandler._setup_action_types.get(action.action_type):
                    results = await setup(action, page, scraped_page, task, step)
                    actions_result.extend(results)
                    if results and results[-1] != ActionSuccess:
                        return actions_result

                # do the handler
                handler = ActionHandler._handled_action_types[action.action_type]
                results = await handler(action, page, scraped_page, task, step)
                actions_result.extend(results)
                if not results or type(actions_result[-1]) != ActionSuccess:
                    return actions_result

                # do the teardown
                teardown = ActionHandler._teardown_action_types.get(action.action_type)
                if not teardown:
                    return actions_result

                results = await teardown(action, page, scraped_page, task, step)
                actions_result.extend(results)
                return actions_result

            else:
                LOG.error(
                    "Unsupported action type in handler",
                    action=action,
                    type=type(action),
                )
                return [ActionFailure(Exception(f"Unsupported action type: {type(action)}"))]
        except MissingElement as e:
            LOG.info(
                "Known exceptions",
                action=action,
                exception_type=type(e),
                exception_message=str(e),
            )
            return [ActionFailure(e)]
        except MultipleElementsFound as e:
            LOG.exception(
                "Cannot handle multiple elements with the same selector in one action.",
                action=action,
            )
            return [ActionFailure(e)]
        except Exception as e:
            LOG.exception("Unhandled exception in action handler", action=action)
            return [ActionFailure(e)]


def check_for_invalid_web_action(
    action: actions.Action,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    if isinstance(action, WebAction) and action.element_id not in scraped_page.id_to_element_dict:
        return [ActionFailure(MissingElement(element_id=action.element_id), stop_execution_on_failure=False)]

    return []


async def handle_solve_captcha_action(
    action: actions.SolveCaptchaAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    LOG.warning(
        "Please solve the captcha on the page, you have 30 seconds",
        action=action,
    )
    await asyncio.sleep(30)
    return [ActionSuccess()]


async def handle_click_action(
    action: actions.ClickAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    num_downloaded_files_before = 0
    download_dir = None
    if task.workflow_run_id:
        download_dir = get_path_for_workflow_download_directory(task.workflow_run_id)
        num_downloaded_files_before = get_number_of_files_in_directory(download_dir)
        LOG.info(
            "Number of files in download directory before click",
            num_downloaded_files_before=num_downloaded_files_before,
            download_dir=download_dir,
        )
    dom = DomUtil(scraped_page=scraped_page, page=page)
    skyvern_element = await dom.get_skyvern_element_by_id(action.element_id)
    await asyncio.sleep(0.3)
    if action.download:
        results = await handle_click_to_download_file_action(action, page, scraped_page)
    else:
        results = await chain_click(
            task,
            scraped_page,
            page,
            action,
            skyvern_element,
            timeout=SettingsManager.get_settings().BROWSER_ACTION_TIMEOUT_MS,
        )

    if results and task.workflow_run_id and download_dir:
        LOG.info("Sleeping for 5 seconds to let the download finish")
        await asyncio.sleep(5)
        num_downloaded_files_after = get_number_of_files_in_directory(download_dir)
        LOG.info(
            "Number of files in download directory after click",
            num_downloaded_files_after=num_downloaded_files_after,
            download_dir=download_dir,
        )
        if num_downloaded_files_after > num_downloaded_files_before:
            results[-1].download_triggered = True

    return results


async def handle_click_to_download_file_action(
    action: actions.ClickAction,
    page: Page,
    scraped_page: ScrapedPage,
) -> list[ActionResult]:
    dom = DomUtil(scraped_page=scraped_page, page=page)
    skyvern_element = await dom.get_skyvern_element_by_id(action.element_id)
    locator = skyvern_element.locator

    try:
        await locator.click(
            timeout=SettingsManager.get_settings().BROWSER_ACTION_TIMEOUT_MS,
            modifiers=["Alt"],
        )
    except Exception as e:
        LOG.exception("ClickAction with download failed", action=action, exc_info=True)
        return [ActionFailure(e, download_triggered=False)]

    return [ActionSuccess()]


async def handle_input_text_action(
    action: actions.InputTextAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    dom = DomUtil(scraped_page, page)
    skyvern_element = await dom.get_skyvern_element_by_id(action.element_id)
    if await skyvern_element.is_select2_dropdown():
        return [ActionFailure(InputActionOnSelect2Dropdown(element_id=action.element_id))]

    current_text = await get_input_value(skyvern_element.get_tag_name(), skyvern_element.get_locator())
    if current_text == action.text:
        return [ActionSuccess()]

    # before filling text, we need to validate if the element can be filled if it's not one of COMMON_INPUT_TAGS
    tag_name = scraped_page.id_to_element_dict[action.element_id]["tagName"].lower()
    text = await get_actual_value_of_parameter_if_secret(task, action.text)
    if text is None:
        return [ActionFailure(FailedToFetchSecret())]

    try:
        await skyvern_element.input_clear()
    except TimeoutError:
        LOG.info("None input tag clear timeout", action=action)
        return [ActionFailure(InvalidElementForTextInput(element_id=action.element_id, tag_name=tag_name))]
    except Exception:
        LOG.warning("Failed to clear the input field", action=action, exc_info=True)
        return [ActionFailure(InvalidElementForTextInput(element_id=action.element_id, tag_name=tag_name))]

    if tag_name not in COMMON_INPUT_TAGS:
        await skyvern_element.input_fill(text)
        return [ActionSuccess()]

    # If the input is a text input, we type the text character by character
    # 3 times the time it takes to type the text so it has time to finish typing
    await skyvern_element.input_sequentially(text=text)
    return [ActionSuccess()]


async def handle_upload_file_action(
    action: actions.UploadFileAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    if not action.file_url:
        LOG.warning("InputFileAction has no file_url", action=action)
        return [ActionFailure(MissingFileUrl())]
    # ************************************************************************************************************** #
    # After this point if the file_url is a secret, it will be replaced with the actual value
    # In order to make sure we don't log the secret value, we log the action with the original value action.file_url
    # ************************************************************************************************************** #
    file_url = await get_actual_value_of_parameter_if_secret(task, action.file_url)
    decoded_url = urllib.parse.unquote(file_url)
    if file_url not in str(task.navigation_payload) and decoded_url not in str(task.navigation_payload):
        LOG.warning(
            "LLM might be imagining the file url, which is not in navigation payload",
            action=action,
            file_url=action.file_url,
        )
        return [ActionFailure(ImaginaryFileUrl(action.file_url))]

    dom = DomUtil(scraped_page=scraped_page, page=page)
    skyvern_element = await dom.get_skyvern_element_by_id(action.element_id)
    locator = skyvern_element.locator

    file_path = await download_file(file_url)
    is_file_input = await is_file_input_element(locator)

    if is_file_input:
        LOG.info("Taking UploadFileAction. Found file input tag", action=action)
        if file_path:
            await locator.set_input_files(
                file_path,
                timeout=SettingsManager.get_settings().BROWSER_ACTION_TIMEOUT_MS,
            )

            # Sleep for 10 seconds after uploading a file to let the page process it
            await asyncio.sleep(10)

            return [ActionSuccess()]
        else:
            return [ActionFailure(Exception(f"Failed to download file from {action.file_url}"))]
    else:
        LOG.info("Taking UploadFileAction. Found non file input tag", action=action)
        # treat it as a click action
        action.is_upload_file_tag = False
        return await chain_click(
            task,
            scraped_page,
            page,
            action,
            skyvern_element,
            timeout=SettingsManager.get_settings().BROWSER_ACTION_TIMEOUT_MS,
        )


@deprecated("This function is deprecated. Downloads are handled by the click action handler now.")
async def handle_download_file_action(
    action: actions.DownloadFileAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    dom = DomUtil(scraped_page=scraped_page, page=page)
    skyvern_element = await dom.get_skyvern_element_by_id(action.element_id)

    file_name = f"{action.file_name or uuid.uuid4()}"
    full_file_path = f"{REPO_ROOT_DIR}/downloads/{task.workflow_run_id or task.task_id}/{file_name}"
    try:
        # Start waiting for the download
        async with page.expect_download() as download_info:
            await asyncio.sleep(0.3)

            locator = skyvern_element.locator
            await locator.click(
                timeout=SettingsManager.get_settings().BROWSER_ACTION_TIMEOUT_MS,
                modifiers=["Alt"],
            )

        download = await download_info.value

        # Create download folders if they don't exist
        download_folder = f"{REPO_ROOT_DIR}/downloads/{task.workflow_run_id or task.task_id}"
        os.makedirs(download_folder, exist_ok=True)
        # Wait for the download process to complete and save the downloaded file
        await download.save_as(full_file_path)
    except Exception as e:
        LOG.exception(
            "DownloadFileAction: Failed to download file",
            action=action,
            full_file_path=full_file_path,
        )
        return [ActionFailure(e)]

    return [ActionSuccess(data={"file_path": full_file_path})]


async def handle_null_action(
    action: actions.NullAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    return [ActionSuccess()]


async def handle_select_option_action(
    action: actions.SelectOptionAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    dom = DomUtil(scraped_page, page)
    skyvern_element = await dom.get_skyvern_element_by_id(action.element_id)

    tag_name = skyvern_element.get_tag_name()
    element_dict = scraped_page.id_to_element_dict[action.element_id]
    LOG.info(
        "SelectOptionAction",
        action=action,
        tag_name=tag_name,
        element_dict=element_dict,
    )

    if not await skyvern_element.is_selectable():
        # 1. find from children
        # TODO: 2. find from siblings and their chidren
        LOG.info(
            "Element is not selectable, try to find the selectable element in the chidren",
            tag_name=tag_name,
            action=action,
        )

        selectable_child: SkyvernElement | None = None
        try:
            selectable_child = await skyvern_element.find_selectable_child(dom=dom)
        except Exception as e:
            LOG.error(
                "Failed to find selectable element in chidren",
                exc_info=True,
                tag_name=tag_name,
                action=action,
            )
            return [ActionFailure(ErrFoundSelectableElement(action.element_id, e))]

        if selectable_child is None:
            LOG.error(
                "No selectable element found in chidren",
                tag_name=tag_name,
                action=action,
            )
            return [ActionFailure(NoSelectableElementFound(action.element_id))]

        LOG.info(
            "Found selectable element in the children",
            tag_name=selectable_child.get_tag_name(),
            element_id=selectable_child.get_id(),
        )
        select_action = SelectOptionAction(element_id=selectable_child.get_id(), option=action.option)
        return await handle_select_option_action(select_action, page, scraped_page, task, step)

    select_framework: AbstractSelectDropdown | None = None

    if await skyvern_element.is_combobox_dropdown():
        LOG.info(
            "This is a combobox dropdown",
            action=action,
        )
        select_framework = await skyvern_element.get_combobox_dropdown()
    if await skyvern_element.is_select2_dropdown():
        LOG.info(
            "This is a select2 dropdown",
            action=action,
        )
        select_framework = await skyvern_element.get_select2_dropdown()

    if select_framework is not None:
        timeout = SettingsManager.get_settings().BROWSER_ACTION_TIMEOUT_MS

        try:
            current_value = await select_framework.get_current_value()
            if current_value == action.option.label or current_value == action.option.value:
                return [ActionSuccess()]
        except Exception:
            LOG.info(
                "failed to confirm if the select option has been done, force to take the action again.",
                exc_info=True,
            )

        await select_framework.open()
        options = await select_framework.get_options()

        result: List[ActionResult] = []
        # select by label first, then by index
        if action.option.label is not None or action.option.value is not None:
            try:
                for option in options:
                    option_content = option.get("text")
                    option_index = option.get("optionIndex", None)
                    if option_index is None:
                        LOG.warning(
                            f"{select_framework.name()} option index is None",
                            option=option,
                        )
                        continue
                    if action.option.label == option_content or action.option.value == option_content:
                        await select_framework.select_by_index(index=option_index, timeout=timeout)
                        result.append(ActionSuccess())
                        return result
                LOG.info(
                    f"no target {select_framework.name()} option matched by label, try to select by index",
                    action=action,
                )
            except Exception as e:
                result.append(ActionFailure(e))
                LOG.info(
                    f"failed to select by label in {select_framework.name()}, try to select by index",
                    exc_info=True,
                    action=action,
                )

        if action.option.index is not None:
            if action.option.index >= len(options):
                result.append(ActionFailure(OptionIndexOutOfBound(action.element_id)))
            else:
                try:
                    option_content = options[action.option.index].get("text")
                    if option_content != action.option.label:
                        LOG.warning(
                            "Select option label is not consistant to the action value. Might select wrong option.",
                            option_content=option_content,
                            action=action,
                        )
                    await select_framework.select_by_index(index=action.option.index, timeout=timeout)
                    result.append(ActionSuccess())
                    return result
                except Exception:
                    result.append(ActionFailure(FailToSelectByIndex(action.element_id)))
                    LOG.info(
                        f"failed to select by index in {select_framework.name()}",
                        exc_info=True,
                        action=action,
                    )

        if len(result) == 0:
            result.append(ActionFailure(EmptySelect(action.element_id)))

        if isinstance(result[-1], ActionFailure):
            LOG.info(
                f"Failed to select a {select_framework.name()} option, close the dropdown",
                action=action,
            )
            await select_framework.close()

        return result

    if await skyvern_element.is_checkbox():
        LOG.info(
            "SelectOptionAction is on <input> checkbox",
            action=action,
        )
        check_action = CheckboxAction(element_id=action.element_id, is_checked=True)
        return await handle_checkbox_action(check_action, page, scraped_page, task, step)

    if await skyvern_element.is_radio():
        LOG.info(
            "SelectOptionAction is on <input> radio",
            action=action,
        )
        click_action = ClickAction(element_id=action.element_id)
        return await chain_click(task, scraped_page, page, click_action, skyvern_element)

    return await normal_select(action=action, skyvern_element=skyvern_element)


async def handle_checkbox_action(
    action: actions.CheckboxAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    """
    ******* NOT REGISTERED *******
    This action causes more harm than it does good.
    It frequently mis-behaves, or gets stuck in click loops.
    Treating checkbox actions as click actions seem to perform way more reliably
    Developers who tried this and failed: 2 (Suchintan and Shu 😂)
    """

    dom = DomUtil(scraped_page=scraped_page, page=page)
    skyvern_element = await dom.get_skyvern_element_by_id(action.element_id)
    locator = skyvern_element.locator

    if action.is_checked:
        await locator.check(timeout=SettingsManager.get_settings().BROWSER_ACTION_TIMEOUT_MS)
    else:
        await locator.uncheck(timeout=SettingsManager.get_settings().BROWSER_ACTION_TIMEOUT_MS)

    # TODO (suchintan): Why does checking the label work, but not the actual input element?
    return [ActionSuccess()]


async def handle_wait_action(
    action: actions.WaitAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    await asyncio.sleep(10)
    return [ActionFailure(exception=Exception("Wait action is treated as a failure"))]


async def handle_terminate_action(
    action: actions.TerminateAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    return [ActionSuccess()]


async def handle_complete_action(
    action: actions.CompleteAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    extracted_data = None
    if action.data_extraction_goal:
        scrape_action_result = await extract_information_for_navigation_goal(
            scraped_page=scraped_page,
            task=task,
            step=step,
        )
        extracted_data = scrape_action_result.scraped_data
    return [ActionSuccess(data=extracted_data)]


ActionHandler.register_action_type(ActionType.SOLVE_CAPTCHA, handle_solve_captcha_action)
ActionHandler.register_action_type(ActionType.CLICK, handle_click_action)
ActionHandler.register_action_type(ActionType.INPUT_TEXT, handle_input_text_action)
ActionHandler.register_action_type(ActionType.UPLOAD_FILE, handle_upload_file_action)
# ActionHandler.register_action_type(ActionType.DOWNLOAD_FILE, handle_download_file_action)
ActionHandler.register_action_type(ActionType.NULL_ACTION, handle_null_action)
ActionHandler.register_action_type(ActionType.SELECT_OPTION, handle_select_option_action)
ActionHandler.register_action_type(ActionType.WAIT, handle_wait_action)
ActionHandler.register_action_type(ActionType.TERMINATE, handle_terminate_action)
ActionHandler.register_action_type(ActionType.COMPLETE, handle_complete_action)


async def get_actual_value_of_parameter_if_secret(task: Task, parameter: str) -> Any:
    """
    Get the actual value of a parameter if it's a secret. If it's not a secret, return the parameter value as is.

    Just return the parameter value if the task isn't a workflow's task.

    This is only used for InputTextAction, UploadFileAction, and ClickAction (if it has a file_url).
    """
    if task.totp_verification_url and task.organization_id and VERIFICATION_CODE_PLACEHOLDER == parameter:
        # if parameter is the secret code in the navigation playload,
        # fetch the real verification from totp_verification_url
        # do polling every 10 seconds to fetch the verification code
        verification_code = await poll_verification_code(task.task_id, task.organization_id, task.totp_verification_url)
        return verification_code

    if task.workflow_run_id is None:
        return parameter

    workflow_run_context = app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context(task.workflow_run_id)
    secret_value = workflow_run_context.get_original_secret_value_or_none(parameter)

    if secret_value == BitwardenConstants.TOTP:
        secrets = workflow_run_context.get_secrets_from_password_manager()
        secret_value = secrets[BitwardenConstants.TOTP]
    return secret_value if secret_value is not None else parameter


async def chain_click(
    task: Task,
    scraped_page: ScrapedPage,
    page: Page,
    action: ClickAction | UploadFileAction,
    skyvern_element: SkyvernElement,
    timeout: int = SettingsManager.get_settings().BROWSER_ACTION_TIMEOUT_MS,
) -> List[ActionResult]:
    # Add a defensive page handler here in case a click action opens a file chooser.
    # This automatically dismisses the dialog
    # File choosers are impossible to close if you don't expect one. Instead of dealing with it, close it!

    locator = skyvern_element.locator
    # TODO (suchintan): This should likely result in an ActionFailure -- we can figure out how to do this later!
    LOG.info("Chain click starts", action=action, locator=locator)
    file: list[str] | str = []
    if action.file_url:
        file_url = await get_actual_value_of_parameter_if_secret(task, action.file_url)
        try:
            file = await download_file(file_url)
        except Exception:
            LOG.exception(
                "Failed to download file, continuing without it",
                action=action,
                file_url=file_url,
            )
            file = []

    is_filechooser_trigger = False

    async def fc_func(fc: FileChooser) -> None:
        await fc.set_files(files=file)
        nonlocal is_filechooser_trigger
        is_filechooser_trigger = True

    page.on("filechooser", fc_func)
    LOG.info("Registered file chooser listener", action=action, path=file)

    """
    Clicks on an element identified by the css and its parent if failed.
    :param css: css of the element to click
    """
    javascript_triggered = await is_javascript_triggered(scraped_page, page, locator)
    try:
        await locator.click(timeout=timeout)

        LOG.info("Chain click: main element click succeeded", action=action, locator=locator)
        return [
            ActionSuccess(
                javascript_triggered=javascript_triggered,
            )
        ]

    except Exception:
        action_results: list[ActionResult] = [
            ActionFailure(
                FailToClick(action.element_id),
                javascript_triggered=javascript_triggered,
            )
        ]
        if await is_input_element(locator):
            LOG.info(
                "Chain click: it's an input element. going to try sibling click",
                action=action,
                locator=locator,
            )
            sibling_action_result = await click_sibling_of_input(locator, timeout=timeout)
            action_results.append(sibling_action_result)
            if type(sibling_action_result) == ActionSuccess:
                return action_results

        try:
            parent_locator = locator.locator("..")

            parent_javascript_triggered = await is_javascript_triggered(scraped_page, page, parent_locator)
            javascript_triggered = javascript_triggered or parent_javascript_triggered

            await parent_locator.click(timeout=timeout)

            LOG.info(
                "Chain click: successfully clicked parent element",
                action=action,
                parent_locator=parent_locator,
            )
            action_results.append(
                ActionSuccess(
                    javascript_triggered=javascript_triggered,
                    interacted_with_parent=True,
                )
            )
        except Exception:
            LOG.warning(
                "Failed to click parent element",
                action=action,
                parent_locator=parent_locator,
                exc_info=True,
            )
            action_results.append(
                ActionFailure(
                    FailToClick(action.element_id),
                    javascript_triggered=javascript_triggered,
                    interacted_with_parent=True,
                )
            )
            # We don't raise exception here because we do log the exception, and return ActionFailure as the last action

        return action_results
    finally:
        LOG.info("Remove file chooser listener", action=action)

        # Sleep for 10 seconds after uploading a file to let the page process it
        # Removing this breaks file uploads using the filechooser
        # KEREM DO NOT REMOVE
        if file:
            await asyncio.sleep(10)
        page.remove_listener("filechooser", fc_func)

        if action.file_url and not is_filechooser_trigger:
            LOG.warning(
                "Action has file_url, but filechoose even hasn't been triggered. Upload file attempt seems to fail",
                action=action,
            )
            return [ActionFailure(WrongElementToUploadFile(action.element_id))]


async def normal_select(
    action: actions.SelectOptionAction,
    skyvern_element: SkyvernElement,
) -> List[ActionResult]:
    try:
        current_text = await skyvern_element.get_attr("selected")
        if current_text == action.option.label or current_text == action.option.value:
            return [ActionSuccess()]
    except Exception:
        LOG.info("failed to confirm if the select option has been done, force to take the action again.")

    action_result: List[ActionResult] = []
    is_success = False
    locator = skyvern_element.get_locator()

    try:
        await locator.click(
            timeout=SettingsManager.get_settings().BROWSER_ACTION_TIMEOUT_MS,
        )
    except Exception as e:
        LOG.error(
            "Failed to click before select action",
            exc_info=True,
            action=action,
            locator=locator,
        )
        action_result.append(ActionFailure(e))
        return action_result

    if not is_success and action.option.label is not None:
        try:
            # First click by label (if it matches)
            await locator.select_option(
                label=action.option.label,
                timeout=SettingsManager.get_settings().BROWSER_ACTION_TIMEOUT_MS,
            )
            is_success = True
            action_result.append(ActionSuccess())
        except Exception:
            action_result.append(ActionFailure(FailToSelectByLabel(action.element_id)))
            LOG.error(
                "Failed to take select action by label",
                exc_info=True,
                action=action,
                locator=locator,
            )

    if not is_success and action.option.value is not None:
        try:
            # click by value (if it matches)
            await locator.select_option(
                value=action.option.value,
                timeout=SettingsManager.get_settings().BROWSER_ACTION_TIMEOUT_MS,
            )
            is_success = True
            action_result.append(ActionSuccess())
        except Exception:
            action_result.append(ActionFailure(FailToSelectByValue(action.element_id)))
            LOG.error(
                "Failed to take select action by value",
                exc_info=True,
                action=action,
                locator=locator,
            )

    if not is_success and action.option.index is not None:
        if action.option.index >= len(skyvern_element.get_options()):
            action_result.append(ActionFailure(OptionIndexOutOfBound(action.element_id)))
            LOG.error(
                "option index is out of bound",
                action=action,
                locator=locator,
            )
        else:
            try:
                # This means the supplied index was for the select element, not a reference to the css dict
                await locator.select_option(
                    index=action.option.index,
                    timeout=SettingsManager.get_settings().BROWSER_ACTION_TIMEOUT_MS,
                )
                is_success = True
                action_result.append(ActionSuccess())
            except Exception:
                action_result.append(ActionFailure(FailToSelectByIndex(action.element_id)))
                LOG.error(
                    "Failed to click on the option by index",
                    exc_info=True,
                    action=action,
                    locator=locator,
                )

    try:
        await locator.click(
            timeout=SettingsManager.get_settings().BROWSER_ACTION_TIMEOUT_MS,
        )
    except Exception as e:
        LOG.error(
            "Failed to click after select action",
            exc_info=True,
            action=action,
            locator=locator,
        )
        action_result.append(ActionFailure(e))
        return action_result

    if len(action_result) == 0:
        action_result.append(ActionFailure(EmptySelect(element_id=action.element_id)))

    return action_result


def get_anchor_to_click(scraped_page: ScrapedPage, element_id: str) -> str | None:
    """
    Get the anchor tag under the label to click
    """
    LOG.info("Getting anchor tag to click", element_id=element_id)
    for ele in scraped_page.elements:
        if "id" in ele and ele["id"] == element_id:
            for child in ele["children"]:
                if "tagName" in child and child["tagName"] == "a":
                    return scraped_page.id_to_css_dict[child["id"]]
    return None


def get_select_id_in_label_children(scraped_page: ScrapedPage, element_id: str) -> str | None:
    """
    search <select> in the children of <label>
    """
    LOG.info("Searching select in the label children", element_id=element_id)
    element = scraped_page.id_to_element_dict.get(element_id, None)
    if element is None:
        return None

    for child in element.get("children", []):
        if child.get("tagName", "") == "select":
            return child.get("id", None)

    return None


def get_checkbox_id_in_label_children(scraped_page: ScrapedPage, element_id: str) -> str | None:
    """
    search checkbox/radio in the children of <label>
    """
    LOG.info("Searching checkbox/radio in the label children", element_id=element_id)
    element = scraped_page.id_to_element_dict.get(element_id, None)
    if element is None:
        return None

    for child in element.get("children", []):
        if child.get("tagName", "") == "input" and child.get("attributes", {}).get("type") in ["checkbox", "radio"]:
            return child.get("id", None)

    return None


@deprecated("This function is deprecated. It was used for select2 dropdown, but we don't use it anymore.")
async def is_javascript_triggered(scraped_page: ScrapedPage, page: Page, locator: Locator) -> bool:
    element = locator.first

    tag_name = await element.evaluate("e => e.tagName")
    if tag_name.lower() == "a":
        href = await element.evaluate("e => e.href")
        if href.lower().startswith("javascript:"):
            LOG.info("Found javascript call in anchor tag, marking step as completed. Dropping remaining actions")
            return True
    return False


async def get_tag_name_lowercase(locator: Locator) -> str | None:
    element = locator.first
    if element:
        tag_name = await element.evaluate("e => e.tagName")
        return tag_name.lower()
    return None


async def is_file_input_element(locator: Locator) -> bool:
    element = locator.first
    if element:
        tag_name = await element.evaluate("el => el.tagName")
        type_name = await element.evaluate("el => el.type")
        return tag_name.lower() == "input" and type_name == "file"
    return False


async def is_input_element(locator: Locator) -> bool:
    element = locator.first
    if element:
        tag_name = await element.evaluate("el => el.tagName")
        return tag_name.lower() == "input"
    return False


async def click_sibling_of_input(
    locator: Locator,
    timeout: int,
    javascript_triggered: bool = False,
) -> ActionResult:
    try:
        input_element = locator.first
        parent_locator = locator.locator("..")
        if input_element:
            input_id = await input_element.get_attribute("id")
            sibling_label_css = f'label[for="{input_id}"]'
            label_locator = parent_locator.locator(sibling_label_css)
            await label_locator.click(timeout=timeout)
            LOG.info(
                "Successfully clicked sibling label of input element",
                sibling_label_css=sibling_label_css,
            )
            return ActionSuccess(javascript_triggered=javascript_triggered, interacted_with_sibling=True)
        # Should never get here
        return ActionFailure(
            exception=Exception("Failed while trying to click sibling of input element"),
            javascript_triggered=javascript_triggered,
            interacted_with_sibling=True,
        )
    except Exception:
        LOG.warning("Failed to click sibling label of input element", exc_info=True)
        return ActionFailure(
            exception=Exception("Failed while trying to click sibling of input element"),
            javascript_triggered=javascript_triggered,
        )


async def extract_information_for_navigation_goal(
    task: Task,
    step: Step,
    scraped_page: ScrapedPage,
) -> ScrapeResult:
    """
    Scrapes a webpage and returns the scraped response, including:
    1. JSON representation of what the user is seeing
    2. The scraped page
    """
    prompt_template = "extract-information"

    # TODO: we only use HTML element for now, introduce a way to switch in the future
    element_tree_format = ElementTreeFormat.HTML
    LOG.info(
        "Building element tree",
        task_id=task.task_id,
        workflow_run_id=task.workflow_run_id,
        format=element_tree_format,
    )

    element_tree_in_prompt: str = scraped_page.build_element_tree(element_tree_format)

    extract_information_prompt = prompt_engine.load_prompt(
        prompt_template,
        navigation_goal=task.navigation_goal,
        navigation_payload=task.navigation_payload,
        elements=element_tree_in_prompt,
        data_extraction_goal=task.data_extraction_goal,
        extracted_information_schema=task.extracted_information_schema,
        current_url=scraped_page.url,
        extracted_text=scraped_page.extracted_text,
        error_code_mapping_str=(json.dumps(task.error_code_mapping) if task.error_code_mapping else None),
        utc_datetime=datetime.utcnow().strftime("%Y-%m-%d %H:%M"),
    )

    json_response = await app.LLM_API_HANDLER(
        prompt=extract_information_prompt,
        step=step,
        screenshots=scraped_page.screenshots,
    )

    return ScrapeResult(
        scraped_data=json_response,
    )


async def click_listbox_option(
    scraped_page: ScrapedPage,
    page: Page,
    action: actions.SelectOptionAction,
    listbox_element_id: str,
) -> bool:
    listbox_element = scraped_page.id_to_element_dict[listbox_element_id]
    # this is a listbox element, get all the children
    if "children" not in listbox_element:
        return False

    LOG.info("starting bfs", listbox_element_id=listbox_element_id)
    bfs_queue = [child for child in listbox_element["children"]]
    while bfs_queue:
        child = bfs_queue.pop(0)
        LOG.info("popped child", element_id=child["id"])
        if "attributes" in child and "role" in child["attributes"] and child["attributes"]["role"] == "option":
            LOG.info("found option", element_id=child["id"])
            text = child["text"] if "text" in child else ""
            if text and (text == action.option.label or text == action.option.value):
                dom = DomUtil(scraped_page=scraped_page, page=page)
                try:
                    skyvern_element = await dom.get_skyvern_element_by_id(child["id"])
                    locator = skyvern_element.locator
                    await locator.click(timeout=1000)

                    return True
                except Exception:
                    LOG.error(
                        "Failed to click on the option",
                        action=action,
                        exc_info=True,
                    )
        if "children" in child:
            bfs_queue.extend(child["children"])
    return False


async def get_input_value(tag_name: str, locator: Locator) -> str | None:
    if tag_name in COMMON_INPUT_TAGS:
        return await locator.input_value()
    # for span, div, p or other tags:
    return await locator.inner_text()


async def poll_verification_code(task_id: str, organization_id: str, url: str) -> str | None:
    timeout = timedelta(minutes=VERIFICATION_CODE_POLLING_TIMEOUT_MINS)
    start_datetime = datetime.utcnow()
    timeout_datetime = start_datetime + timeout
    org_token = await app.DATABASE.get_valid_org_auth_token(organization_id, OrganizationAuthTokenType.api)
    if not org_token:
        LOG.error("Failed to get organization token when trying to get verification code")
        return None
    while True:
        # check timeout
        if datetime.utcnow() > timeout_datetime:
            return None
        request_data = {
            "task_id": task_id,
        }
        payload = json.dumps(request_data)
        signature = generate_skyvern_signature(
            payload=payload,
            api_key=org_token.token,
        )
        timestamp = str(int(datetime.utcnow().timestamp()))
        headers = {
            "x-skyvern-timestamp": timestamp,
            "x-skyvern-signature": signature,
            "Content-Type": "application/json",
        }
        json_resp = await aiohttp_post(url=url, data=request_data, headers=headers, raise_exception=False)
        verification_code = json_resp.get("verification_code", None)
        if verification_code:
            LOG.info("Got verification code", verification_code=verification_code)
            return verification_code

        await asyncio.sleep(10)
