import asyncio
import json
import os
import uuid
from typing import Any, Awaitable, Callable, List

import structlog
from deprecation import deprecated
from playwright.async_api import Locator, Page, TimeoutError

from skyvern.constants import INPUT_TEXT_TIMEOUT, REPO_ROOT_DIR
from skyvern.exceptions import (
    EmptySelect,
    FailToSelectByIndex,
    FailToSelectByLabel,
    FailToSelectByValue,
    ImaginaryFileUrl,
    InputActionOnSelect2Dropdown,
    InvalidElementForTextInput,
    MissingElement,
    MissingFileUrl,
    MultipleElementsFound,
    OptionIndexOutOfBound,
)
from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.api.files import (
    download_file,
    get_number_of_files_in_directory,
    get_path_for_workflow_download_directory,
)
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
from skyvern.webeye.scraper.scraper import ScrapedPage
from skyvern.webeye.utils.dom import DomUtil, InteractiveElement, Select2Dropdown, SkyvernElement, resolve_locator

LOG = structlog.get_logger()
TEXT_INPUT_DELAY = 10  # 10ms between each character input
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
                "Cannot handle multiple elements with the same xpath in one action.",
                action=action,
            )
            return [ActionFailure(e)]
        except Exception as e:
            LOG.exception("Unhandled exception in action handler", action=action)
            return [ActionFailure(e)]


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
    xpath, frame = await validate_actions_in_dom(action, page, scraped_page)
    await asyncio.sleep(0.3)
    if action.download:
        results = await handle_click_to_download_file_action(action, page, scraped_page)
    else:
        results = await chain_click(
            task,
            scraped_page,
            page,
            action,
            xpath,
            frame,
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
    xpath, frame = await validate_actions_in_dom(action, page, scraped_page)

    locator = resolve_locator(scraped_page, page, frame, xpath)

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

    xpath, frame = await validate_actions_in_dom(action, page, scraped_page)

    locator = resolve_locator(scraped_page, page, frame, xpath)

    current_text = await get_input_value(locator)
    if current_text == action.text:
        return [ActionSuccess()]

    # before filling text, we need to validate if the element can be filled if it's not one of COMMON_INPUT_TAGS
    tag_name = scraped_page.id_to_element_dict[action.element_id]["tagName"].lower()
    text = get_actual_value_of_parameter_if_secret(task, action.text)

    try:
        await locator.clear(timeout=SettingsManager.get_settings().BROWSER_ACTION_TIMEOUT_MS)
    except TimeoutError:
        LOG.info("None input tag clear timeout", action=action)
        return [ActionFailure(InvalidElementForTextInput(element_id=action.element_id, tag_name=tag_name))]
    except Exception:
        LOG.warning("Failed to clear the input field", action=action, exc_info=True)
        return [ActionFailure(InvalidElementForTextInput(element_id=action.element_id, tag_name=tag_name))]

    if tag_name not in COMMON_INPUT_TAGS:
        await locator.fill(text, timeout=SettingsManager.get_settings().BROWSER_ACTION_TIMEOUT_MS)
        return [ActionSuccess()]

    # If the input is a text input, we type the text character by character
    # 3 times the time it takes to type the text so it has time to finish typing
    await locator.press_sequentially(text, timeout=INPUT_TEXT_TIMEOUT)
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
    file_url = get_actual_value_of_parameter_if_secret(task, action.file_url)
    if file_url not in str(task.navigation_payload):
        LOG.warning(
            "LLM might be imagining the file url, which is not in navigation payload",
            action=action,
            file_url=action.file_url,
        )
        return [ActionFailure(ImaginaryFileUrl(action.file_url))]

    xpath, frame = await validate_actions_in_dom(action, page, scraped_page)

    file_path = await download_file(file_url)

    locator = resolve_locator(scraped_page, page, frame, xpath)

    is_file_input = await is_file_input_element(locator)

    if is_file_input:
        LOG.info("Taking UploadFileAction. Found file input tag", action=action)
        if file_path:
            locator = resolve_locator(scraped_page, page, frame, xpath)

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
            xpath,
            frame,
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
    xpath, frame = await validate_actions_in_dom(action, page, scraped_page)
    file_name = f"{action.file_name or uuid.uuid4()}"
    full_file_path = f"{REPO_ROOT_DIR}/downloads/{task.workflow_run_id or task.task_id}/{file_name}"
    try:
        # Start waiting for the download
        async with page.expect_download() as download_info:
            await asyncio.sleep(0.3)

            locator = resolve_locator(scraped_page, page, frame, xpath)

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

    xpath, frame = await validate_actions_in_dom(action, page, scraped_page)

    locator = resolve_locator(scraped_page, page, frame, xpath)

    tag_name = await get_tag_name_lowercase(locator)
    element_dict = scraped_page.id_to_element_dict[action.element_id]
    LOG.info(
        "SelectOptionAction",
        action=action,
        tag_name=tag_name,
        element_dict=element_dict,
    )

    # if element is not a select option, prioritize clicking the linked element if any
    if tag_name != "select" and "linked_element" in element_dict:
        LOG.info(
            "SelectOptionAction is not on a select tag and found a linked element",
            action=action,
            linked_element=element_dict["linked_element"],
        )
        listbox_click_success = await click_listbox_option(scraped_page, page, action, element_dict["linked_element"])
        if listbox_click_success:
            LOG.info(
                "Successfully clicked linked element",
                action=action,
                linked_element=element_dict["linked_element"],
            )
            return [ActionSuccess()]
        LOG.warning(
            "Failed to click linked element",
            action=action,
            linked_element=element_dict["linked_element"],
        )

    # check if the element is an a tag first. If yes, click it instead of selecting the option
    if tag_name == "label":
        # label pointed to select2 <a> element
        select2_element_id: str | None = None
        # search <a> anchor first and then search <input> anchor
        select2_element_id = skyvern_element.find_element_id_in_label_children(InteractiveElement.A)
        if select2_element_id is None:
            select2_element_id = skyvern_element.find_element_id_in_label_children(InteractiveElement.INPUT)

        if select2_element_id is not None:
            select2_skyvern_element = await dom.get_skyvern_element_by_id(element_id=select2_element_id)
            if await select2_skyvern_element.is_select2_dropdown():
                LOG.info(
                    "SelectOptionAction is on <label>. take the action on the real select2 element",
                    action=action,
                    select2_element_id=select2_element_id,
                )
                select_action = SelectOptionAction(element_id=select2_element_id, option=action.option)
                return await handle_select_option_action(select_action, page, scraped_page, task, step)

        # handler the select action on <label>
        if select_element_id := get_select_id_in_label_children(scraped_page, action.element_id):
            LOG.info(
                "SelectOptionAction is on <label>. take the action on the real <select>",
                action=action,
                select_element_id=select_element_id,
            )
            select_action = SelectOptionAction(element_id=select_element_id, option=action.option)
            return await handle_select_option_action(select_action, page, scraped_page, task, step)

        # handle the select action on <label> of checkbox/radio
        if checkbox_element_id := get_checkbox_id_in_label_children(scraped_page, action.element_id):
            LOG.info(
                "SelectOptionAction is on <label> of <input> checkbox/radio. take the action on the real <input> checkbox/radio",
                action=action,
                checkbox_element_id=checkbox_element_id,
            )
            select_action = SelectOptionAction(element_id=checkbox_element_id, option=action.option)
            return await handle_select_option_action(select_action, page, scraped_page, task, step)

        return [ActionFailure(Exception("No element pointed by the label found"))]
    elif await skyvern_element.is_select2_dropdown():
        LOG.info(
            "This is a select2 dropdown",
            action=action,
        )
        timeout = SettingsManager.get_settings().BROWSER_ACTION_TIMEOUT_MS

        select2_element = Select2Dropdown(page=page, skyvern_element=skyvern_element)

        await select2_element.open()
        options = await select2_element.get_options()

        result: List[ActionResult] = []
        # select by label first, then by index
        if action.option.label is not None or action.option.value is not None:
            try:
                for option in options:
                    option_content = option.get("text")
                    option_index = option.get("optionIndex", None)
                    if option_index is None:
                        LOG.warning(
                            "Select2 option index is None",
                            option=option,
                        )
                        continue
                    if action.option.label == option_content or action.option.value == option_content:
                        await select2_element.select_by_index(index=option_index, timeout=timeout)
                        result.append(ActionSuccess())
                        return result
                LOG.info(
                    "no target select2 option matched by label, try to select by index",
                    action=action,
                )
            except Exception as e:
                result.append(ActionFailure(e))
                LOG.info(
                    "failed to select by label in select2, try to select by index",
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
                    await select2_element.select_by_index(index=action.option.index, timeout=timeout)
                    result.append(ActionSuccess())
                    return result
                except Exception:
                    result.append(ActionFailure(FailToSelectByIndex(action.element_id)))
                    LOG.info(
                        "failed to select by index in select2",
                        exc_info=True,
                        action=action,
                    )

        if len(result) == 0:
            result.append(ActionFailure(EmptySelect(action.element_id)))

        if isinstance(result[-1], ActionFailure):
            LOG.info(
                "Failed to select a select2 option, close the dropdown",
                action=action,
            )
            await select2_element.close()

        return result
    elif tag_name == "ul" or tag_name == "div" or tag_name == "li":
        # if the role is listbox, find the option with the "label" or "value" and click that option element
        # references:
        # https://developer.mozilla.org/en-US/docs/Web/Accessibility/ARIA/Roles/listbox_role
        # https://developer.mozilla.org/en-US/docs/Web/Accessibility/ARIA/Roles/option_role
        role_attribute = await locator.get_attribute("role")
        if role_attribute == "listbox":
            LOG.info(
                "SelectOptionAction on a listbox element. Searching for the option and click it",
                action=action,
            )
            # use playwright to click the option
            # clickOption is defined in domUtils.js
            option_locator = locator.locator('[role="option"]')
            option_num = await option_locator.count()
            if action.option.index and action.option.index < option_num:
                try:
                    await option_locator.nth(action.option.index).click(timeout=2000)
                    return [ActionSuccess()]
                except Exception as e:
                    LOG.error("Failed to click option", action=action, exc_info=True)
                    return [ActionFailure(e)]
            return [ActionFailure(Exception("SelectOption option index is missing"))]
        elif role_attribute == "option":
            LOG.info(
                "SelectOptionAction on an option element. Clicking the option",
                action=action,
            )
            # click the option element
            click_action = ClickAction(element_id=action.element_id)
            return await chain_click(task, scraped_page, page, click_action, xpath, frame)
        else:
            LOG.error(
                "SelectOptionAction on a non-listbox element. Cannot handle this action",
            )
            return [ActionFailure(Exception("Cannot handle SelectOptionAction on a non-listbox element"))]
    elif await skyvern_element.is_checkbox():
        LOG.info(
            "SelectOptionAction is on <input> checkbox",
            action=action,
        )
        check_action = CheckboxAction(element_id=action.element_id, is_checked=True)
        return await handle_checkbox_action(check_action, page, scraped_page, task, step)
    elif await skyvern_element.is_radio():
        LOG.info(
            "SelectOptionAction is on <input> radio",
            action=action,
        )
        click_action = ClickAction(element_id=action.element_id)
        return await chain_click(task, scraped_page, page, click_action, xpath, frame)

    try:
        current_text = await locator.input_value()
        if current_text == action.option.label or current_text == action.option.value:
            return [ActionSuccess()]
    except Exception:
        LOG.info("failed to confirm if the select option has been done, force to take the action again.")

    return await normal_select(action=action, skyvern_element=skyvern_element, xpath=xpath, frame=frame)


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
    xpath, frame = await validate_actions_in_dom(action, page, scraped_page)

    locator = resolve_locator(scraped_page, page, frame, xpath)

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


def get_actual_value_of_parameter_if_secret(task: Task, parameter: str) -> Any:
    """
    Get the actual value of a parameter if it's a secret. If it's not a secret, return the parameter value as is.

    Just return the parameter value if the task isn't a workflow's task.

    This is only used for InputTextAction, UploadFileAction, and ClickAction (if it has a file_url).
    """
    if task.workflow_run_id is None:
        return parameter

    workflow_run_context = app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context(task.workflow_run_id)
    secret_value = workflow_run_context.get_original_secret_value_or_none(parameter)

    if secret_value == BitwardenConstants.TOTP:
        secrets = workflow_run_context.get_secrets_from_password_manager()
        secret_value = secrets[BitwardenConstants.TOTP]
    return secret_value if secret_value is not None else parameter


async def validate_actions_in_dom(action: WebAction, page: Page, scraped_page: ScrapedPage) -> tuple[str, str]:
    xpath = scraped_page.id_to_xpath_dict[action.element_id]
    frame = scraped_page.id_to_frame_dict[action.element_id]

    locator = resolve_locator(scraped_page, page, frame, xpath)

    num_elements = await locator.count()
    if num_elements < 1:
        LOG.warning(
            "No elements found with action xpath. Validation failed.",
            action=action,
            xpath=xpath,
        )
        raise MissingElement(xpath=xpath, element_id=action.element_id)
    elif num_elements > 1:
        LOG.warning(
            "Multiple elements found with action xpath. Expected 1. Validation failed.",
            action=action,
            num_elements=num_elements,
        )
        raise MultipleElementsFound(num=num_elements, xpath=xpath, element_id=action.element_id)
    else:
        LOG.info("Validated action xpath in DOM", action=action)

    return xpath, frame


async def chain_click(
    task: Task,
    scraped_page: ScrapedPage,
    page: Page,
    action: ClickAction | UploadFileAction,
    xpath: str,
    frame: str,
    timeout: int = SettingsManager.get_settings().BROWSER_ACTION_TIMEOUT_MS,
) -> List[ActionResult]:
    # Add a defensive page handler here in case a click action opens a file chooser.
    # This automatically dismisses the dialog
    # File choosers are impossible to close if you don't expect one. Instead of dealing with it, close it!

    # TODO (suchintan): This should likely result in an ActionFailure -- we can figure out how to do this later!
    LOG.info("Chain click starts", action=action, xpath=xpath)
    file: list[str] | str = []
    if action.file_url:
        file_url = get_actual_value_of_parameter_if_secret(task, action.file_url)
        try:
            file = await download_file(file_url)
        except Exception:
            LOG.exception(
                "Failed to download file, continuing without it",
                action=action,
                file_url=file_url,
            )
            file = []

    fc_func = lambda fc: fc.set_files(files=file)  # noqa: E731
    page.on("filechooser", fc_func)
    LOG.info("Registered file chooser listener", action=action, path=file)

    """
    Clicks on an element identified by the xpath and its parent if failed.
    :param xpath: xpath of the element to click
    """
    javascript_triggered = await is_javascript_triggered(scraped_page, page, frame, xpath)
    locator = resolve_locator(scraped_page, page, frame, xpath)
    try:
        await locator.click(timeout=timeout)

        LOG.info("Chain click: main element click succeeded", action=action, xpath=xpath)
        return [
            ActionSuccess(
                javascript_triggered=javascript_triggered,
            )
        ]
    except Exception as e:
        action_results: list[ActionResult] = [
            ActionFailure(
                e,
                javascript_triggered=javascript_triggered,
            )
        ]
        if await is_input_element(locator):
            LOG.info(
                "Chain click: it's an input element. going to try sibling click",
                action=action,
                xpath=xpath,
            )
            sibling_action_result = await click_sibling_of_input(locator, timeout=timeout)
            action_results.append(sibling_action_result)
            if type(sibling_action_result) == ActionSuccess:
                return action_results

        parent_xpath = f"{xpath}/.."
        try:
            parent_javascript_triggered = await is_javascript_triggered(scraped_page, page, frame, parent_xpath)
            javascript_triggered = javascript_triggered or parent_javascript_triggered

            parent_locator = resolve_locator(scraped_page, page, frame, xpath).locator("..")
            await parent_locator.click(timeout=timeout)

            LOG.info(
                "Chain click: successfully clicked parent element",
                action=action,
                parent_xpath=parent_xpath,
            )
            action_results.append(
                ActionSuccess(
                    javascript_triggered=javascript_triggered,
                    interacted_with_parent=True,
                )
            )
        except Exception as pe:
            LOG.warning(
                "Failed to click parent element",
                action=action,
                parent_xpath=parent_xpath,
                exc_info=True,
            )
            action_results.append(
                ActionFailure(
                    pe,
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


async def normal_select(
    action: actions.SelectOptionAction,
    skyvern_element: SkyvernElement,
    xpath: str,
    frame: str,
) -> List[ActionResult]:
    action_result: List[ActionResult] = []
    is_success = False
    locator = skyvern_element.locator

    try:
        await locator.click(
            timeout=SettingsManager.get_settings().BROWSER_ACTION_TIMEOUT_MS,
        )
    except Exception as e:
        LOG.error(
            "Failed to click before select action",
            exc_info=True,
            action=action,
            xpath=xpath,
            frame=frame,
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
                xpath=xpath,
                frame=frame,
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
                xpath=xpath,
                frame=frame,
            )

    if not is_success and action.option.index is not None:
        if action.option.index >= len(skyvern_element.get_options()):
            action_result.append(ActionFailure(OptionIndexOutOfBound(action.element_id)))
            LOG.error(
                "option index is out of bound",
                action=action,
                xpath=xpath,
                frame=frame,
            )
        else:
            try:
                # This means the supplied index was for the select element, not a reference to the xpath dict
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
                    xpath=xpath,
                    frame=frame,
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
            xpath=xpath,
            frame=frame,
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
                    return scraped_page.id_to_xpath_dict[child["id"]]
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


async def is_javascript_triggered(scraped_page: ScrapedPage, page: Page, frame: str, xpath: str) -> bool:
    locator = resolve_locator(scraped_page, page, frame, xpath)
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
            sibling_label_xpath = f'//label[@for="{input_id}"]'
            label_locator = parent_locator.locator(sibling_label_xpath)
            await label_locator.click(timeout=timeout)
            LOG.info(
                "Successfully clicked sibling label of input element",
                sibling_label_xpath=sibling_label_xpath,
            )
            return ActionSuccess(javascript_triggered=javascript_triggered, interacted_with_sibling=True)
        # Should never get here
        return ActionFailure(
            exception=Exception("Failed while trying to click sibling of input element"),
            javascript_triggered=javascript_triggered,
            interacted_with_sibling=True,
        )
    except Exception as e:
        LOG.warning("Failed to click sibling label of input element", exc_info=True)
        return ActionFailure(exception=e, javascript_triggered=javascript_triggered)


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

    extract_information_prompt = prompt_engine.load_prompt(
        prompt_template,
        navigation_goal=task.navigation_goal,
        navigation_payload=task.navigation_payload,
        elements=scraped_page.element_tree,
        data_extraction_goal=task.data_extraction_goal,
        extracted_information_schema=task.extracted_information_schema,
        current_url=scraped_page.url,
        extracted_text=scraped_page.extracted_text,
        error_code_mapping_str=(json.dumps(task.error_code_mapping) if task.error_code_mapping else None),
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
                option_xpath = scraped_page.id_to_xpath_dict[child["id"]]
                option_frame = scraped_page.id_to_frame_dict[child["id"]]

                try:
                    locator = resolve_locator(scraped_page, page, option_frame, option_xpath)

                    await locator.click(timeout=1000)

                    return True
                except Exception:
                    LOG.error(
                        "Failed to click on the option",
                        action=action,
                        option_xpath=option_xpath,
                        exc_info=True,
                    )
        if "children" in child:
            bfs_queue.extend(child["children"])
    return False


async def get_input_value(locator: Locator) -> str | None:
    tag_name = await get_tag_name_lowercase(locator)
    if tag_name in COMMON_INPUT_TAGS:
        return await locator.input_value()
    # for span, div, p or other tags:
    return await locator.inner_text()
