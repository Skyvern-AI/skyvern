from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Callable, Literal

import structlog
from jinja2.sandbox import SandboxedEnvironment
from playwright.async_api import Page

from skyvern.config import settings
from skyvern.constants import SPECIAL_FIELD_VERIFICATION_CODE
from skyvern.core.totp import poll_verification_code
from skyvern.exceptions import WorkflowRunNotFound
from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.api.files import download_file
from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.core import skyvern_context
from skyvern.utils.prompt_engine import load_prompt_with_elements
from skyvern.webeye.actions import handler_utils
from skyvern.webeye.actions.action_types import ActionType
from skyvern.webeye.actions.actions import Action, ActionStatus, ExtractAction, InputTextAction, SelectOption
from skyvern.webeye.actions.handler import handle_input_text_action, handle_select_option_action
from skyvern.webeye.actions.parse_actions import parse_actions
from skyvern.webeye.browser_factory import BrowserState
from skyvern.webeye.scraper.scraper import ScrapedPage, scrape_website

jinja_sandbox_env = SandboxedEnvironment()
LOG = structlog.get_logger()
SELECT_OPTION_GOAL = """- The intention to select an option: {intention}.
- The overall goal that the user wants to achieve: {prompt}."""


class Driver(StrEnum):
    PLAYWRIGHT = "playwright"


@dataclass
class ActionMetadata:
    intention: str = ""
    data: dict[str, Any] | str | None = None
    timestamp: float | None = None  # filled in by recorder
    screenshot_path: str | None = None  # if enabled


@dataclass
class ActionCall:
    name: ActionType
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    meta: ActionMetadata
    result: Any | None = None  # populated after execution
    error: Exception | None = None  # populated if failed


async def _get_element_id_by_xpath(xpath: str, page: Page) -> str | None:
    locator = page.locator(f"xpath={xpath}")
    element_id = await locator.get_attribute("unique_id")
    return element_id


def _get_context_data(data: str | dict[str, Any] | None = None) -> dict[str, Any] | str | None:
    context = skyvern_context.current()
    global_context_data = context.script_run_parameters if context else None
    if not data:
        return global_context_data
    result: dict[str, Any] | str | None
    if isinstance(data, dict):
        result = {k: v for k, v in data.items() if v}
        if global_context_data:
            result.update(global_context_data)
    else:
        global_context_data_str = json.dumps(global_context_data) if global_context_data else ""
        result = f"{data}\n{global_context_data_str}"
    return result


def _render_template_with_label(template: str, label: str | None = None) -> str:
    template_data = {}
    context = skyvern_context.current()
    if context and context.workflow_run_id:
        workflow_run_context = app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context(context.workflow_run_id)
        block_reference_data: dict[str, Any] = workflow_run_context.get_block_metadata(label)
        template_data = workflow_run_context.values.copy()
        if label in template_data:
            current_value = template_data[label]
            if isinstance(current_value, dict):
                block_reference_data.update(current_value)
            else:
                LOG.warning(
                    f"Script service: Parameter {label} has a registered reference value, going to overwrite it by block metadata"
                )

        if label:
            template_data[label] = block_reference_data

        # inject the forloop metadata as global variables
        if "current_index" in block_reference_data:
            template_data["current_index"] = block_reference_data["current_index"]
        if "current_item" in block_reference_data:
            template_data["current_item"] = block_reference_data["current_item"]
        if "current_value" in block_reference_data:
            template_data["current_value"] = block_reference_data["current_value"]
    return render_template(template, data=template_data)


def render_template(template: str, data: dict[str, Any] | None = None) -> str:
    """
    Refer to  Block.format_block_parameter_template_from_workflow_run_context

    TODO: complete this function so that block code shares the same template rendering logic
    """
    template_data = data.copy() if data else {}
    jinja_template = jinja_sandbox_env.from_string(template)
    context = skyvern_context.current()
    if context and context.workflow_run_id:
        workflow_run_id = context.workflow_run_id
        workflow_run_context = app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context(workflow_run_id)
        template_data.update(workflow_run_context.values)
        if template in template_data:
            return template_data[template]

    return jinja_template.render(template_data)


class SkyvernPage:
    """
    A minimal adapter around the chosen driver that:
    1. Executes real browser commands
    2. Records ActionCallobjects into RunContext.trace
    3. Adds retry / fallback hooks
    """

    def __init__(
        self,
        scraped_page: ScrapedPage,
        page: Page,
        *,
        recorder: Callable[[ActionCall], None] | None = None,
        # generate_response: bool = False,
    ):
        self.scraped_page = scraped_page
        self.page = page
        self._record = recorder or (lambda ac: None)
        self.current_label: str | None = None

    @classmethod
    async def _get_or_create_browser_state(cls, browser_session_id: str | None = None) -> BrowserState:
        context = skyvern_context.current()
        if context and context.workflow_run_id and context.organization_id:
            workflow_run = await app.DATABASE.get_workflow_run(
                workflow_run_id=context.workflow_run_id, organization_id=context.organization_id
            )
            if workflow_run:
                browser_state = await app.BROWSER_MANAGER.get_or_create_for_workflow_run(
                    workflow_run=workflow_run,
                    browser_session_id=browser_session_id,
                )
            else:
                raise WorkflowRunNotFound(workflow_run_id=context.workflow_run_id)
        else:
            browser_state = await app.BROWSER_MANAGER.get_or_create_for_script(browser_session_id=browser_session_id)
        return browser_state

    @classmethod
    async def _get_browser_state(cls) -> BrowserState | None:
        context = skyvern_context.current()
        if context and context.workflow_run_id and context.organization_id:
            workflow_run = await app.DATABASE.get_workflow_run(
                workflow_run_id=context.workflow_run_id, organization_id=context.organization_id
            )
            if workflow_run:
                browser_state = app.BROWSER_MANAGER.get_for_workflow_run(workflow_run_id=context.workflow_run_id)
            else:
                raise WorkflowRunNotFound(workflow_run_id=context.workflow_run_id)
        else:
            browser_state = app.BROWSER_MANAGER.get_for_script()
        return browser_state

    @classmethod
    async def create(
        cls,
        browser_session_id: str | None = None,
    ) -> SkyvernPage:
        # initialize browser state
        # TODO: add workflow_run_id or eventually script_id/script_run_id
        browser_state = await cls._get_or_create_browser_state(browser_session_id=browser_session_id)
        scraped_page = await scrape_website(
            browser_state=browser_state,
            url="",
            cleanup_element_tree=app.AGENT_FUNCTION.cleanup_element_tree_factory(),
            scrape_exclude=app.scrape_exclude,
            max_screenshot_number=settings.MAX_NUM_SCREENSHOTS,
            draw_boxes=True,
            scroll=True,
            support_empty_page=True,
        )
        page = await scraped_page._browser_state.must_get_working_page()
        return cls(scraped_page=scraped_page, page=page)

    @staticmethod
    def action_wrap(
        action: ActionType,
    ) -> Callable:
        """
        Decorator to record the action call.

        Auto-creates action records in DB before action execution
        and screenshot artifacts after action execution.
        """

        def decorator(fn: Callable) -> Callable:
            async def wrapper(
                skyvern_page: SkyvernPage,
                *args: Any,
                intention: str = "",
                data: str | dict[str, Any] = "",
                **kwargs: Any,
            ) -> Any:
                meta = ActionMetadata(intention, data)
                call = ActionCall(action, args, kwargs, meta)

                action_status = ActionStatus.completed

                try:
                    call.result = await fn(
                        skyvern_page, *args, intention=intention, data=data, **kwargs
                    )  # real driver call

                    # Note: Action status would be updated to completed here if update method existed

                    return call.result
                except Exception as e:
                    call.error = e
                    action_status = ActionStatus.failed
                    # Note: Action status would be updated to failed here if update method existed

                    # LLM fallback hook could go here ...
                    raise
                finally:
                    skyvern_page._record(call)
                    # Auto-create action after execution
                    await skyvern_page._create_action_after_execution(
                        action_type=action,
                        intention=intention,
                        status=action_status,
                        data=data,
                        kwargs=kwargs,
                        call_result=call.result,
                    )

                    # Auto-create screenshot artifact after execution
                    await skyvern_page._create_screenshot_after_execution()

            return wrapper

        return decorator

    async def goto(self, url: str, timeout: float = settings.BROWSER_LOADING_TIMEOUT_MS) -> None:
        url = render_template(url)
        await self.page.goto(
            url,
            timeout=timeout,
        )

    async def _create_action_after_execution(
        self,
        action_type: ActionType,
        intention: str = "",
        status: ActionStatus = ActionStatus.pending,
        data: str | dict[str, Any] = "",
        kwargs: dict[str, Any] | None = None,
        call_result: Any | None = None,
    ) -> Action | None:
        """Create an action record in the database before execution if task_id and step_id are available."""
        try:
            context = skyvern_context.current()
            if not context or not context.task_id or not context.step_id:
                return None

            # Create action record. TODO: store more action fields
            kwargs = kwargs or {}
            # we're using "value" instead of "text" for input text actions interface
            text = None
            select_option = None
            response: str | None = kwargs.get("response")
            file_url = kwargs.get("file_url")
            if not response:
                if action_type == ActionType.INPUT_TEXT:
                    text = str(call_result)
                    response = text
                elif action_type == ActionType.SELECT_OPTION:
                    option_value = str(call_result) or ""
                    select_option = SelectOption(value=option_value)
                    response = option_value
                elif action_type == ActionType.UPLOAD_FILE:
                    file_url = str(call_result)

            action = Action(
                element_id="",
                action_type=action_type,
                status=status,
                organization_id=context.organization_id,
                workflow_run_id=context.workflow_run_id,
                task_id=context.task_id,
                step_id=context.step_id,
                step_order=0,  # Will be updated by the system if needed
                action_order=context.action_order,  # Will be updated by the system if needed
                intention=intention,
                reasoning=f"Auto-generated action for {action_type.value}",
                text=text,
                option=select_option,
                file_url=file_url,
                response=response,
                created_by="script",
            )
            if action_type == ActionType.EXTRACT:
                action = ExtractAction(
                    element_id="",
                    action_type=action_type,
                    status=status,
                    organization_id=context.organization_id,
                    workflow_run_id=context.workflow_run_id,
                    task_id=context.task_id,
                    step_id=context.step_id,
                    step_order=0,
                    action_order=context.action_order,
                    intention=intention,
                    reasoning=f"Auto-generated action for {action_type.value}",
                    data_extraction_goal=kwargs.get("prompt"),
                    data_extraction_schema=kwargs.get("schema"),
                    option=select_option,
                    response=response,
                    created_by="script",
                )

            created_action = await app.DATABASE.create_action(action)
            context.action_order += 1

            return created_action

        except Exception:
            # If action creation fails, don't block the actual action execution
            return None

    @classmethod
    async def _create_screenshot_after_execution(cls) -> None:
        """Create a screenshot artifact after action execution if task_id and step_id are available."""
        try:
            context = skyvern_context.ensure_context()
            if not context or not context.task_id or not context.step_id:
                return

            # Get browser state and take screenshot
            browser_state = await cls._get_browser_state()
            if not browser_state:
                return

            screenshot = await browser_state.take_post_action_screenshot(scrolling_number=0)

            if screenshot:
                # Create a minimal Step object for artifact creation
                step = await app.DATABASE.get_step(
                    context.step_id,
                    organization_id=context.organization_id,
                )
                if not step:
                    return

                await app.ARTIFACT_MANAGER.create_artifact(
                    step=step,
                    artifact_type=ArtifactType.SCREENSHOT_ACTION,
                    data=screenshot,
                )

        except Exception:
            # If screenshot creation fails, don't block execution
            pass

    ######### Public Interfaces #########
    @action_wrap(ActionType.CLICK)
    async def click(self, xpath: str, intention: str | None = None, data: str | dict[str, Any] | None = None) -> None:
        """Click an element identified by ``xpath``.

        When ``intention`` and ``data`` are provided a new click action is
        generated via the ``single-click-action`` prompt.  The model returns a
        fresh xpath based on the current DOM and the updated data for this run.
        The browser then clicks the element using this newly generated xpath.

        If the prompt generation or parsing fails for any reason we fall back to
        clicking the originally supplied ``xpath``.
        """
        new_xpath = xpath

        if intention:
            try:
                # Build the element tree of the current page for the prompt
                context = skyvern_context.ensure_context()
                payload_str = _get_context_data(data)
                refreshed_page = await self.scraped_page.generate_scraped_page_without_screenshots()
                element_tree = refreshed_page.build_element_tree()
                single_click_prompt = prompt_engine.load_prompt(
                    template="single-click-action",
                    navigation_goal=intention,
                    navigation_payload_str=payload_str,
                    current_url=self.page.url,
                    elements=element_tree,
                    local_datetime=datetime.now(context.tz_info or datetime.now().astimezone().tzinfo).isoformat(),
                    # user_context=getattr(context, "prompt", None),
                )
                json_response = await app.SINGLE_CLICK_AGENT_LLM_API_HANDLER(
                    prompt=single_click_prompt,
                    prompt_name="single-click-action",
                    organization_id=context.organization_id,
                )
                actions = json_response.get("actions", [])
                if actions:
                    new_xpath = actions[0].get("xpath", xpath) or xpath
            except Exception:
                # If anything goes wrong, fall back to the original xpath
                new_xpath = xpath

        locator = self.page.locator(f"xpath={new_xpath}")
        await locator.click(timeout=5000)

    @action_wrap(ActionType.INPUT_TEXT)
    async def fill(
        self,
        xpath: str,
        value: str,
        ai_infer: bool = False,
        intention: str | None = None,
        data: str | dict[str, Any] | None = None,
        timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS,
        totp_identifier: str | None = None,
        totp_url: str | None = None,
    ) -> str:
        return await self._input_text(
            xpath=xpath,
            value=value,
            ai_infer=ai_infer,
            intention=intention,
            data=data,
            timeout=timeout,
            totp_identifier=totp_identifier,
            totp_url=totp_url,
        )

    @action_wrap(ActionType.INPUT_TEXT)
    async def type(
        self,
        xpath: str,
        value: str,
        ai_infer: bool = False,
        intention: str | None = None,
        data: str | dict[str, Any] | None = None,
        timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS,
        totp_identifier: str | None = None,
        totp_url: str | None = None,
    ) -> str:
        return await self._input_text(
            xpath=xpath,
            value=value,
            ai_infer=ai_infer,
            intention=intention,
            data=data,
            timeout=timeout,
            totp_identifier=totp_identifier,
            totp_url=totp_url,
        )

    async def _input_text(
        self,
        xpath: str,
        value: str,
        ai_infer: bool = False,
        intention: str | None = None,
        data: str | dict[str, Any] | None = None,
        timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS,
        totp_identifier: str | None = None,
        totp_url: str | None = None,
    ) -> str:
        """Input text into an element identified by ``xpath``.

        When ``intention`` and ``data`` are provided a new input text action is
        generated via the `script-generation-input-text-generatiion` prompt.  The model returns a
        fresh text based on the current DOM and the updated data for this run.
        The browser then inputs the text using this newly generated text.

        If the prompt generation or parsing fails for any reason we fall back to
        inputting the originally supplied ``text``.
        """
        # format the text with the actual value of the parameter if it's a secret when running a workflow
        context = skyvern_context.current()
        value = value or ""
        transformed_value = value
        element_id: str | None = None
        organization_id = context.organization_id if context else None
        task_id = context.task_id if context else None
        step_id = context.step_id if context else None
        workflow_run_id = context.workflow_run_id if context else None
        task = await app.DATABASE.get_task(task_id, organization_id) if task_id and organization_id else None
        step = await app.DATABASE.get_step(step_id, organization_id) if step_id and organization_id else None
        if ai_infer and intention:
            try:
                prompt = context.prompt if context else None
                data = data or {}
                if (totp_identifier or totp_url) and context and organization_id and task_id:
                    verification_code = await poll_verification_code(
                        organization_id=organization_id,
                        task_id=task_id,
                        workflow_run_id=workflow_run_id,
                        totp_identifier=totp_identifier,
                        totp_verification_url=totp_url,
                    )
                    if verification_code:
                        if isinstance(data, dict) and SPECIAL_FIELD_VERIFICATION_CODE not in data:
                            data[SPECIAL_FIELD_VERIFICATION_CODE] = verification_code
                        elif isinstance(data, str) and SPECIAL_FIELD_VERIFICATION_CODE not in data:
                            data = f"{data}\n" + str({SPECIAL_FIELD_VERIFICATION_CODE: verification_code})
                        elif isinstance(data, list):
                            data.append({SPECIAL_FIELD_VERIFICATION_CODE: verification_code})
                        else:
                            data = {SPECIAL_FIELD_VERIFICATION_CODE: verification_code}

                refreshed_page = await self.scraped_page.generate_scraped_page_without_screenshots()
                self.scraped_page = refreshed_page
                # get the element_id by the xpath
                element_id = await _get_element_id_by_xpath(xpath, self.page)
                script_generation_input_text_prompt = prompt_engine.load_prompt(
                    template="script-generation-input-text-generatiion",
                    intention=intention,
                    goal=prompt,
                )
                json_response = await app.SINGLE_INPUT_AGENT_LLM_API_HANDLER(
                    prompt=script_generation_input_text_prompt,
                    prompt_name="script-generation-input-text-generatiion",
                    organization_id=organization_id,
                )
                value = json_response.get("answer", value)
            except Exception:
                LOG.exception(f"Failed to adapt value for input text action on xpath={xpath}, value={value}")

        if context and context.workflow_run_id:
            transformed_value = await _get_actual_value_of_parameter_if_secret(context.workflow_run_id, value)

        if element_id and organization_id and task and step:
            action = InputTextAction(
                element_id=element_id,
                text=value,
                status=ActionStatus.pending,
                organization_id=organization_id,
                workflow_run_id=workflow_run_id,
                task_id=task_id,
                step_id=context.step_id if context else None,
                reasoning=intention,
                intention=intention,
                response=value,
            )
            await handle_input_text_action(action, self.page, self.scraped_page, task, step)
        else:
            locator = self.page.locator(f"xpath={xpath}")
            await handler_utils.input_sequentially(locator, transformed_value, timeout=timeout)
        return value

    @action_wrap(ActionType.UPLOAD_FILE)
    async def upload_file(
        self,
        xpath: str,
        files: str,
        ai_infer: bool = False,
        intention: str | None = None,
        data: str | dict[str, Any] | None = None,
    ) -> str:
        if ai_infer and intention:
            try:
                context = skyvern_context.current()
                prompt = context.prompt if context else None
                data = _get_context_data(data)
                script_generation_file_url_prompt = prompt_engine.load_prompt(
                    template="script-generation-file-url-generation",
                    intention=intention,
                    data=data,
                    goal=prompt,
                )
                json_response = await app.SINGLE_INPUT_AGENT_LLM_API_HANDLER(
                    prompt=script_generation_file_url_prompt,
                    prompt_name="script-generation-file-url-generation",
                    organization_id=context.organization_id if context else None,
                )
                files = json_response.get("answer", files)
            except Exception:
                LOG.exception(f"Failed to adapt value for input text action on xpath={xpath}, file={files}")
        file_path = await download_file(files)
        locator = self.page.locator(f"xpath={xpath}")
        await locator.set_input_files(file_path)
        return files

    @action_wrap(ActionType.SELECT_OPTION)
    async def select_option(
        self,
        xpath: str,
        value: str,
        ai_infer: bool = False,
        intention: str | None = None,
        data: str | dict[str, Any] | None = None,
        timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS,
    ) -> str:
        option_value = value or ""
        context = skyvern_context.current()
        if context and context.task_id and context.step_id and context.organization_id:
            task = await app.DATABASE.get_task(context.task_id, organization_id=context.organization_id)
            step = await app.DATABASE.get_step(context.step_id, organization_id=context.organization_id)
            if ai_infer and intention and task and step:
                try:
                    prompt = context.prompt if context else None
                    # data = _get_context_data(data)
                    data = data or {}
                    refreshed_page = await self.scraped_page.generate_scraped_page_without_screenshots()
                    self.scraped_page = refreshed_page
                    element_tree = refreshed_page.build_element_tree()
                    merged_goal = SELECT_OPTION_GOAL.format(intention=intention, prompt=prompt)
                    single_select_prompt = prompt_engine.load_prompt(
                        template="single-select-action",
                        navigation_payload_str=data,
                        navigation_goal=merged_goal,
                        current_url=self.page.url,
                        elements=element_tree,
                        local_datetime=datetime.now(context.tz_info or datetime.now().astimezone().tzinfo).isoformat(),
                    )
                    json_response = await app.SELECT_AGENT_LLM_API_HANDLER(
                        prompt=single_select_prompt,
                        prompt_name="single-select-action",
                        organization_id=context.organization_id if context else None,
                    )
                    actions = parse_actions(task, step.step_id, step.order, self.scraped_page, json_response["actions"])
                    if actions:
                        action = actions[0]
                        if not action.option:
                            raise ValueError("SelectOptionAction requires an 'option' field")
                        option_value = action.option.value or action.option.label or ""
                        await handle_select_option_action(
                            action=action,
                            page=self.page,
                            scraped_page=self.scraped_page,
                            task=task,
                            step=step,
                        )
                    else:
                        LOG.exception(
                            f"Failed to parse actions for select option action on xpath={xpath}, value={value}"
                        )
                except Exception:
                    LOG.exception(f"Failed to adapt value for select option action on xpath={xpath}, value={value}")
        else:
            locator = self.page.locator(f"xpath={xpath}")
            await locator.select_option(option_value, timeout=timeout)
        return option_value

    @action_wrap(ActionType.WAIT)
    async def wait(
        self, seconds: float, intention: str | None = None, data: str | dict[str, Any] | None = None
    ) -> None:
        await asyncio.sleep(seconds)

    @action_wrap(ActionType.NULL_ACTION)
    async def null_action(self, intention: str | None = None, data: str | dict[str, Any] | None = None) -> None:
        return

    @action_wrap(ActionType.SOLVE_CAPTCHA)
    async def solve_captcha(
        self, xpath: str, intention: str | None = None, data: str | dict[str, Any] | None = None
    ) -> None:
        await asyncio.sleep(30)

    @action_wrap(ActionType.TERMINATE)
    async def terminate(
        self, errors: list[str], intention: str | None = None, data: str | dict[str, Any] | None = None
    ) -> None:
        # TODO: update the workflow run status to terminated
        return

    @action_wrap(ActionType.COMPLETE)
    async def complete(
        self, data_extraction_goal: str, intention: str | None = None, data: str | dict[str, Any] | None = None
    ) -> None:
        # TODO: update the workflow run status to completed
        return

    @action_wrap(ActionType.RELOAD_PAGE)
    async def reload_page(self, intention: str | None = None, data: str | dict[str, Any] | None = None) -> None:
        await self.page.reload()
        return

    @action_wrap(ActionType.EXTRACT)
    async def extract(
        self,
        prompt: str,
        schema: dict[str, Any] | list | str | None = None,
        error_code_mapping: dict[str, str] | None = None,
        intention: str | None = None,
        data: str | dict[str, Any] | None = None,
    ) -> dict[str, Any] | list | str | None:
        scraped_page_refreshed = await self.scraped_page.refresh()
        context = skyvern_context.current()
        tz_info = datetime.now(tz=timezone.utc).tzinfo
        if context and context.tz_info:
            tz_info = context.tz_info
        prompt = _render_template_with_label(prompt, label=self.current_label)
        extract_information_prompt = load_prompt_with_elements(
            element_tree_builder=scraped_page_refreshed,
            prompt_engine=prompt_engine,
            template_name="extract-information",
            html_need_skyvern_attrs=False,
            data_extraction_goal=prompt,
            extracted_information_schema=schema,
            current_url=scraped_page_refreshed.url,
            extracted_text=scraped_page_refreshed.extracted_text,
            error_code_mapping_str=(json.dumps(error_code_mapping) if error_code_mapping else None),
            local_datetime=datetime.now(tz_info).isoformat(),
        )
        step = None
        if context and context.organization_id and context.task_id and context.step_id:
            step = await app.DATABASE.get_step(
                step_id=context.step_id,
                organization_id=context.organization_id,
            )

        result = await app.EXTRACTION_LLM_API_HANDLER(
            prompt=extract_information_prompt,
            step=step,
            screenshots=scraped_page_refreshed.screenshots,
            prompt_name="extract-information",
        )
        return result

    @action_wrap(ActionType.VERIFICATION_CODE)
    async def verification_code(
        self, xpath: str, intention: str | None = None, data: str | dict[str, Any] | None = None
    ) -> None:
        return

    @action_wrap(ActionType.SCROLL)
    async def scroll(
        self, scroll_x: int, scroll_y: int, intention: str | None = None, data: str | dict[str, Any] | None = None
    ) -> None:
        await self.page.evaluate(f"window.scrollBy({scroll_x}, {scroll_y})")

    @action_wrap(ActionType.KEYPRESS)
    async def keypress(
        self,
        keys: list[str],
        hold: bool = False,
        duration: float = 0,
        intention: str | None = None,
        data: str | dict[str, Any] | None = None,
    ) -> None:
        await handler_utils.keypress(self.page, keys, hold=hold, duration=duration)

    @action_wrap(ActionType.MOVE)
    async def move(
        self, x: int, y: int, intention: str | None = None, data: str | dict[str, Any] | None = None
    ) -> None:
        await self.page.mouse.move(x, y)

    @action_wrap(ActionType.DRAG)
    async def drag(
        self,
        start_x: int,
        start_y: int,
        path: list[tuple[int, int]],
        intention: str | None = None,
        data: str | dict[str, Any] | None = None,
    ) -> None:
        await handler_utils.drag(self.page, start_x, start_y, path)

    @action_wrap(ActionType.LEFT_MOUSE)
    async def left_mouse(
        self,
        x: int,
        y: int,
        direction: Literal["down", "up"],
        intention: str | None = None,
        data: str | dict[str, Any] | None = None,
    ) -> None:
        await handler_utils.left_mouse(self.page, x, y, direction)


class RunContext:
    def __init__(
        self, parameters: dict[str, Any], page: SkyvernPage, generated_parameters: dict[str, Any] | None = None
    ) -> None:
        self.original_parameters = parameters
        self.generated_parameters = generated_parameters
        self.parameters = copy.deepcopy(parameters)
        if generated_parameters:
            # hydrate the generated parameter fields in the run context parameters
            for key, value in generated_parameters.items():
                if key not in self.parameters:
                    self.parameters[key] = value
        self.page = page
        self.trace: list[ActionCall] = []


async def _get_actual_value_of_parameter_if_secret(workflow_run_id: str, parameter: str) -> Any:
    """
    Get the actual value of a parameter if it's a secret. If it's not a secret, return the parameter value as is.

    Just return the parameter value if the task isn't a workflow's task.

    This is only used for InputTextAction, UploadFileAction, and ClickAction (if it has a file_url).
    """
    workflow_run_context = app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context(workflow_run_id)
    secret_value = workflow_run_context.get_original_secret_value_or_none(parameter)
    return secret_value if secret_value is not None else parameter


class ScriptRunContextManager:
    """
    Manages the run context for code runs.
    """

    def __init__(self) -> None:
        # self.run_contexts: dict[str, RunContext] = {}
        self.run_context: RunContext | None = None
        self.cached_fns: dict[str, Callable] = {}

    def get_run_context(self) -> RunContext | None:
        return self.run_context

    def set_run_context(self, run_context: RunContext) -> None:
        self.run_context = run_context

    def ensure_run_context(self) -> RunContext:
        if not self.run_context:
            raise Exception("Run context not found")
        return self.run_context

    def set_cached_fn(self, cache_key: str, fn: Callable) -> None:
        self.cached_fns[cache_key] = fn

    def get_cached_fn(self, cache_key: str | None = None) -> Callable | None:
        if cache_key:
            return self.cached_fns.get(cache_key)
        return None


script_run_context_manager = ScriptRunContextManager()
