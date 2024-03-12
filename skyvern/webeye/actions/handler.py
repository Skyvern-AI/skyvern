import asyncio
import re
from typing import Awaitable, Callable, List

import structlog
from playwright.async_api import Locator, Page

from skyvern.exceptions import ImaginaryFileUrl, MissingElement, MissingFileUrl, MultipleElementsFound
from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.api.files import download_file
from skyvern.forge.sdk.models import Step
from skyvern.forge.sdk.schemas.tasks import Task
from skyvern.forge.sdk.settings_manager import SettingsManager
from skyvern.webeye.actions import actions
from skyvern.webeye.actions.actions import Action, ActionType, ClickAction, ScrapeResult, UploadFileAction, WebAction
from skyvern.webeye.actions.responses import ActionFailure, ActionResult, ActionSuccess
from skyvern.webeye.browser_factory import BrowserState
from skyvern.webeye.scraper.scraper import ScrapedPage

LOG = structlog.get_logger()


class ActionHandler:
    _handled_action_types: dict[
        ActionType, Callable[[Action, Page, ScrapedPage, Task, Step], Awaitable[list[ActionResult]]]
    ] = {}

    @classmethod
    def register_action_type(
        cls,
        action_type: ActionType,
        handler: Callable[[Action, Page, ScrapedPage, Task, Step], Awaitable[list[ActionResult]]],
    ) -> None:
        cls._handled_action_types[action_type] = handler

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
                handler = ActionHandler._handled_action_types[action.action_type]
                return await handler(action, page, scraped_page, task, step)
            else:
                LOG.error("Unsupported action type in handler", action=action, type=type(action))
                return [ActionFailure(Exception(f"Unsupported action type: {type(action)}"))]
        except MissingElement as e:
            LOG.info("Known exceptions", action=action, exception_type=type(e), exception_message=str(e))
            return [ActionFailure(e)]
        except MultipleElementsFound as e:
            LOG.exception(
                "Cannot handle multiple elements with the same xpath in one action.",
                action=action,
                exception=e,
            )
            return [ActionFailure(e)]
        except Exception as e:
            LOG.exception("Unhandled exception in action handler", action=action, exception=e)
            return [ActionFailure(e)]


async def handle_solve_captcha_action(
    action: actions.SolveCaptchaAction, page: Page, scraped_page: ScrapedPage, task: Task, step: Step
) -> list[ActionResult]:
    LOG.warning(
        "Please solve the captcha on the page, you have 30 seconds",
        action=action,
    )
    await asyncio.sleep(30)
    return [ActionSuccess()]


async def handle_click_action(
    action: actions.ClickAction, page: Page, scraped_page: ScrapedPage, task: Task, step: Step
) -> list[ActionResult]:
    xpath = await validate_actions_in_dom(action, page, scraped_page)
    await asyncio.sleep(0.3)
    return await chain_click(page, action, xpath, timeout=SettingsManager.get_settings().BROWSER_ACTION_TIMEOUT_MS)


async def handle_input_text_action(
    action: actions.InputTextAction, page: Page, scraped_page: ScrapedPage, task: Task, step: Step
) -> list[ActionResult]:
    xpath = await validate_actions_in_dom(action, page, scraped_page)
    locator = page.locator(f"xpath={xpath}")
    await locator.clear()
    await locator.fill(action.text, timeout=SettingsManager.get_settings().BROWSER_ACTION_TIMEOUT_MS)

    # This is a hack that gets dropdowns to select the "best" option based on what's typed
    # Fixes situations like tsk_228671423990405776 where the location isn't being autocompleted
    await locator.press("Tab", timeout=SettingsManager.get_settings().BROWSER_ACTION_TIMEOUT_MS)
    input_value = await locator.input_value(timeout=SettingsManager.get_settings().BROWSER_ACTION_TIMEOUT_MS)
    if not input_value:
        LOG.info("Failed to input the text, trying to press sequentially with an enter click", action=action)
        await locator.clear()
        await locator.press_sequentially(action.text, timeout=SettingsManager.get_settings().BROWSER_ACTION_TIMEOUT_MS)
        await locator.press("Enter", timeout=SettingsManager.get_settings().BROWSER_ACTION_TIMEOUT_MS)
        input_value = await locator.input_value(timeout=SettingsManager.get_settings().BROWSER_ACTION_TIMEOUT_MS)
        LOG.info("Input value", input_value=input_value, action=action)

    return [ActionSuccess()]


async def handle_upload_file_action(
    action: actions.UploadFileAction, page: Page, scraped_page: ScrapedPage, task: Task, step: Step
) -> list[ActionResult]:
    if not action.file_url:
        LOG.warning("InputFileAction has no file_url", action=action)
        return [ActionFailure(MissingFileUrl())]
    if action.file_url not in str(task.navigation_payload):
        LOG.warning(
            "LLM might be imagining the file url, which is not in navigation payload",
            action=action,
            file_url=action.file_url,
        )
        return [ActionFailure(ImaginaryFileUrl(action.file_url))]
    xpath = await validate_actions_in_dom(action, page, scraped_page)
    file_path = download_file(action.file_url)
    locator = page.locator(f"xpath={xpath}")
    is_file_input = await is_file_input_element(locator)
    if is_file_input:
        LOG.info("Taking UploadFileAction. Found file input tag", action=action)
        if file_path:
            await page.locator(f"xpath={xpath}").set_input_files(
                file_path, timeout=SettingsManager.get_settings().BROWSER_ACTION_TIMEOUT_MS
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
        return await chain_click(page, action, xpath, timeout=SettingsManager.get_settings().BROWSER_ACTION_TIMEOUT_MS)


async def handle_null_action(
    action: actions.NullAction, page: Page, scraped_page: ScrapedPage, task: Task, step: Step
) -> list[ActionResult]:
    return [ActionSuccess()]


async def handle_select_option_action(
    action: actions.SelectOptionAction, page: Page, scraped_page: ScrapedPage, task: Task, step: Step
) -> list[ActionResult]:
    xpath = await validate_actions_in_dom(action, page, scraped_page)

    locator = page.locator(f"xpath={xpath}")
    tag_name = await get_tag_name_lowercase(locator)
    element_dict = scraped_page.id_to_element_dict[action.element_id]
    LOG.info("SelectOptionAction", action=action, tag_name=tag_name, element_dict=element_dict)

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
        LOG.warning("Failed to click linked element", action=action, linked_element=element_dict["linked_element"])

    # check if the element is an a tag first. If yes, click it instead of selecting the option
    if tag_name == "label":
        # TODO: this is a hack to handle the case where the label is the only thing that's clickable
        # it's a label, look for the anchor tag
        child_anchor_xpath = get_anchor_to_click(scraped_page, action.element_id)
        if child_anchor_xpath:
            LOG.info(
                "SelectOptionAction is a label tag. Clicking the anchor tag instead of selecting the option",
                action=action,
                child_anchor_xpath=child_anchor_xpath,
            )
            click_action = ClickAction(element_id=action.element_id)
            return await chain_click(page, click_action, child_anchor_xpath)
        return [ActionFailure(Exception("No anchor tag found for the label for SelectOptionAction"))]
    elif tag_name == "a":
        # turn the SelectOptionAction into a ClickAction
        LOG.info(
            "SelectOptionAction is an anchor tag. Clicking it instead of selecting the option",
            action=action,
        )
        click_action = ClickAction(element_id=action.element_id)
        action_result = await chain_click(page, click_action, xpath)
        return action_result
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
                    LOG.error(
                        "Failed to click option",
                        action=action,
                        exception=e,
                    )
                    return [ActionFailure(e)]
            return [ActionFailure(Exception(f"SelectOption option index is missing"))]
        elif role_attribute == "option":
            LOG.info(
                "SelectOptionAction on an option element. Clicking the option",
                action=action,
            )
            # click the option element
            click_action = ClickAction(element_id=action.element_id)
            return await chain_click(page, click_action, xpath)
        else:
            LOG.error(
                "SelectOptionAction on a non-listbox element. Cannot handle this action",
            )
            return [ActionFailure(Exception(f"Cannot handle SelectOptionAction on a non-listbox element"))]

    try:
        # First click by label (if it matches)
        await page.click(f"xpath={xpath}", timeout=SettingsManager.get_settings().BROWSER_ACTION_TIMEOUT_MS)
        await page.select_option(
            xpath,
            label=action.option.label,
            timeout=SettingsManager.get_settings().BROWSER_ACTION_TIMEOUT_MS,
        )
        await page.click(f"xpath={xpath}", timeout=SettingsManager.get_settings().BROWSER_ACTION_TIMEOUT_MS)
        return [ActionSuccess()]
    except Exception as e:
        if action.option.index is not None:
            LOG.warning(
                "Failed to click on the option by label, trying by index",
                exc_info=e,
                action=action,
                xpath=xpath,
            )
        else:
            return [ActionFailure(e)]

    try:
        option_xpath = scraped_page.id_to_xpath_dict[action.option.index]
        match = re.search(r"option\[(\d+)]$", option_xpath)
        if match:
            # This means we were trying to select an option xpath, click the option
            option_index = int(match.group(1))
            await page.click(f"xpath={xpath}", timeout=SettingsManager.get_settings().BROWSER_ACTION_TIMEOUT_MS)
            await page.select_option(
                xpath,
                index=option_index,
                timeout=SettingsManager.get_settings().BROWSER_ACTION_TIMEOUT_MS,
            )
            await page.click(f"xpath={xpath}", timeout=SettingsManager.get_settings().BROWSER_ACTION_TIMEOUT_MS)
            return [ActionSuccess()]
        else:
            # This means the supplied index was for the select element, not a reference to the xpath dict
            await page.click(f"xpath={xpath}", timeout=SettingsManager.get_settings().BROWSER_ACTION_TIMEOUT_MS)
            await page.select_option(
                xpath,
                index=action.option.index,
                timeout=SettingsManager.get_settings().BROWSER_ACTION_TIMEOUT_MS,
            )
            await page.click(f"xpath={xpath}", timeout=SettingsManager.get_settings().BROWSER_ACTION_TIMEOUT_MS)
        return [ActionSuccess()]
    except Exception as e:
        LOG.warning("Failed to click on the option by index", exception=e, action=action)
        return [ActionFailure(e)]


async def handle_checkbox_action(
    self: actions.CheckboxAction, page: Page, scraped_page: ScrapedPage, task: Task, step: Step
) -> list[ActionResult]:
    """
    ******* NOT REGISTERED *******
    This action causes more harm than it does good.
    It frequently mis-behaves, or gets stuck in click loops.
    Treating checkbox actions as click actions seem to perform way more reliably
    Developers who tried this and failed: 2 (Suchintan and Shu 😂)
    """
    xpath = await validate_actions_in_dom(self, page, scraped_page)
    if self.is_checked:
        await page.check(xpath, timeout=SettingsManager.get_settings().BROWSER_ACTION_TIMEOUT_MS)
    else:
        await page.uncheck(xpath, timeout=SettingsManager.get_settings().BROWSER_ACTION_TIMEOUT_MS)

    # TODO (suchintan): Why does checking the label work, but not the actual input element?
    return [ActionSuccess()]


async def handle_wait_action(
    action: actions.WaitAction, page: Page, scraped_page: ScrapedPage, task: Task, step: Step
) -> list[ActionResult]:
    await asyncio.sleep(10)
    return [ActionFailure(exception=Exception("Wait action is treated as a failure"))]


async def handle_terminate_action(
    action: actions.TerminateAction, page: Page, scraped_page: ScrapedPage, task: Task, step: Step
) -> list[ActionResult]:
    return [ActionSuccess()]


async def handle_complete_action(
    action: actions.CompleteAction, page: Page, scraped_page: ScrapedPage, task: Task, step: Step
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
ActionHandler.register_action_type(ActionType.NULL_ACTION, handle_null_action)
ActionHandler.register_action_type(ActionType.SELECT_OPTION, handle_select_option_action)
ActionHandler.register_action_type(ActionType.WAIT, handle_wait_action)
ActionHandler.register_action_type(ActionType.TERMINATE, handle_terminate_action)
ActionHandler.register_action_type(ActionType.COMPLETE, handle_complete_action)


async def validate_actions_in_dom(action: WebAction, page: Page, scraped_page: ScrapedPage) -> str:
    xpath = scraped_page.id_to_xpath_dict[action.element_id]
    locator = page.locator(xpath)

    num_elements = await locator.count()
    if num_elements < 1:
        LOG.warning("No elements found with action xpath. Validation failed.", action=action, xpath=xpath)
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

    return xpath


async def chain_click(
    page: Page,
    action: ClickAction | UploadFileAction,
    xpath: str,
    timeout: int = SettingsManager.get_settings().BROWSER_ACTION_TIMEOUT_MS,
) -> List[ActionResult]:
    # Add a defensive page handler here in case a click action opens a file chooser.
    # This automatically dismisses the dialog
    # File choosers are impossible to close if you don't expect one. Instead of dealing with it, close it!

    # TODO (suchintan): This should likely result in an ActionFailure -- we can figure out how to do this later!
    LOG.info("Chain click starts", action=action, xpath=xpath)
    file: list[str] | str = []
    if action.file_url:
        file = download_file(action.file_url) or []

    fc_func = lambda fc: fc.set_files(files=file)
    page.on("filechooser", fc_func)

    LOG.info("Registered file chooser listener", action=action, path=file)
    """
    Clicks on an element identified by the xpath and its parent if failed.
    :param xpath: xpath of the element to click
    """
    javascript_triggered = await is_javascript_triggered(page, xpath)
    try:
        await page.click(f"xpath={xpath}", timeout=timeout)
        LOG.info("Chain click: main element click succeeded", action=action, xpath=xpath)
        return [ActionSuccess(javascript_triggered=javascript_triggered)]
    except Exception as e:
        action_results: list[ActionResult] = [ActionFailure(e, javascript_triggered=javascript_triggered)]
        if await is_input_element(page.locator(xpath)):
            LOG.info("Chain click: it's an input element. going to try sibling click", action=action, xpath=xpath)
            sibling_action_result = await click_sibling_of_input(page.locator(xpath), timeout=timeout)
            action_results.append(sibling_action_result)
            if type(sibling_action_result) == ActionSuccess:
                return action_results

        parent_xpath = f"{xpath}/.."
        try:
            parent_javascript_triggered = await is_javascript_triggered(page, parent_xpath)
            javascript_triggered = javascript_triggered or parent_javascript_triggered
            parent_locator = page.locator(xpath).locator("..")
            await parent_locator.click(timeout=timeout)
            LOG.info("Chain click: successfully clicked parent element", action=action, parent_xpath=parent_xpath)
            action_results.append(
                ActionSuccess(
                    javascript_triggered=javascript_triggered,
                    interacted_with_parent=True,
                )
            )
        except Exception as pe:
            LOG.warning("Failed to click parent element", action=action, parent_xpath=parent_xpath, exc_info=True)
            action_results.append(
                ActionFailure(pe, javascript_triggered=javascript_triggered, interacted_with_parent=True)
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


def get_anchor_to_click(scraped_page: ScrapedPage, element_id: int) -> str | None:
    """
    Get the anchor tag under the label to click
    """
    LOG.info("Getting anchor tag to click", element_id=element_id)
    element_id = int(element_id)
    for ele in scraped_page.elements:
        if "id" in ele and ele["id"] == element_id:
            for child in ele["children"]:
                if "tagName" in child and child["tagName"] == "a":
                    return scraped_page.id_to_xpath_dict[child["id"]]
    return None


async def is_javascript_triggered(page: Page, xpath: str) -> bool:
    locator = page.locator(f"xpath={xpath}")
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
        LOG.warning("Failed to click sibling label of input element", exc_info=e)
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
        elements=scraped_page.element_tree,
        data_extraction_goal=task.data_extraction_goal,
        extracted_information_schema=task.extracted_information_schema,
        current_url=scraped_page.url,
        extracted_text=scraped_page.extracted_text,
    )

    json_response = await app.OPENAI_CLIENT.chat_completion(
        step=step,
        prompt=extract_information_prompt,
        screenshots=scraped_page.screenshots,
    )

    return ScrapeResult(
        scraped_data=json_response,
    )


async def click_listbox_option(
    scraped_page: ScrapedPage,
    page: Page,
    action: actions.SelectOptionAction,
    listbox_element_id: int,
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
                try:
                    await page.click(f"xpath={option_xpath}", timeout=1000)
                    return True
                except Exception as e:
                    LOG.error(
                        "Failed to click on the option",
                        action=action,
                        option_xpath=option_xpath,
                        exception=e,
                    )
        if "children" in child:
            bfs_queue.extend(child["children"])
    return False
