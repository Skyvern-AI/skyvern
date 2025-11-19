from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, cast

import structlog
from jinja2.sandbox import SandboxedEnvironment
from playwright.async_api import Page

from skyvern.config import settings
from skyvern.constants import SPECIAL_FIELD_VERIFICATION_CODE
from skyvern.core.script_generations.skyvern_page_ai import SkyvernPageAi
from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.api.files import validate_download_url
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.schemas.totp_codes import OTPType
from skyvern.services.otp_service import poll_otp_value
from skyvern.utils.prompt_engine import load_prompt_with_elements
from skyvern.webeye.actions import handler_utils
from skyvern.webeye.actions.actions import (
    ActionStatus,
    ClickAction,
    InputTextAction,
    SelectOptionAction,
    UploadFileAction,
)
from skyvern.webeye.actions.handler import (
    handle_click_action,
    handle_input_text_action,
    handle_select_option_action,
    handle_upload_file_action,
)
from skyvern.webeye.actions.parse_actions import parse_actions
from skyvern.webeye.scraper.scraper import ScrapedPage

jinja_sandbox_env = SandboxedEnvironment()

LOG = structlog.get_logger()

INPUT_GOAL = """- The intention to fill out an input: {intention}.
- The overall goal that the user wants to achieve: {prompt}."""

SELECT_OPTION_GOAL = """- The intention to select an option: {intention}.
- The overall goal that the user wants to achieve: {prompt}."""

UPLOAD_GOAL = """- The intention to upload a file: {intention}.
- The overall goal that the user wants to achieve: {prompt}."""


async def _get_element_id_by_selector(selector: str, page: Page) -> str | None:
    locator = page.locator(selector)
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
    try:
        return render_template(template, data=template_data)
    except Exception:
        LOG.exception("Failed to render template", template=template, data=template_data)
        return template


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


class RealSkyvernPageAi(SkyvernPageAi):
    def __init__(
        self,
        scraped_page: ScrapedPage,
        page: Page,
    ):
        self.scraped_page = scraped_page
        self.page = page
        self.current_label: str | None = None

    async def ai_click(
        self,
        selector: str | None,
        intention: str,
        data: str | dict[str, Any] | None = None,
        timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS,
    ) -> str | None:
        """Click an element using AI to locate it based on intention."""
        try:
            # Build the element tree of the current page for the prompt
            context = skyvern_context.ensure_context()
            payload_str = _get_context_data(data)
            refreshed_page = await self.scraped_page.generate_scraped_page_without_screenshots()
            element_tree = refreshed_page.build_element_tree()

            organization_id = context.organization_id if context else None
            step_id = context.step_id if context else None
            step = await app.DATABASE.get_step(step_id, organization_id) if step_id and organization_id else None
            single_click_prompt = prompt_engine.load_prompt(
                template="single-click-action",
                navigation_goal=intention,
                navigation_payload_str=payload_str,
                current_url=self.page.url,
                elements=element_tree,
                local_datetime=datetime.now(context.tz_info or datetime.now().astimezone().tzinfo).isoformat(),
                user_context=context.prompt,
            )
            json_response = await app.SINGLE_CLICK_AGENT_LLM_API_HANDLER(
                prompt=single_click_prompt,
                prompt_name="single-click-action",
                step=step,
                organization_id=context.organization_id,
            )
            actions_json = json_response.get("actions", [])
            if actions_json:
                task_id = context.task_id if context else None
                task = await app.DATABASE.get_task(task_id, organization_id) if task_id and organization_id else None
                if organization_id and task and step:
                    actions = parse_actions(
                        task, step.step_id, step.order, self.scraped_page, json_response.get("actions", [])
                    )
                    action = cast(ClickAction, actions[0])
                    result = await handle_click_action(action, self.page, self.scraped_page, task, step)
                    if result and result[-1].success is False:
                        raise Exception(result[-1].exception_message)
                    xpath = action.get_xpath()
                    selector = f"xpath={xpath}" if xpath else selector
                    return selector
        except Exception:
            LOG.exception(
                f"Failed to do ai click. Falling back to original selector={selector}, intention={intention}, data={data}"
            )

        if selector:
            locator = self.page.locator(selector)
            await locator.click(timeout=timeout)
        return selector

    async def ai_input_text(
        self,
        selector: str | None,
        value: str | None,
        intention: str,
        data: str | dict[str, Any] | None = None,
        totp_identifier: str | None = None,
        totp_url: str | None = None,
        timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS,
    ) -> str:
        """Input text into an element using AI to determine the value."""

        context = skyvern_context.ensure_context()
        value = value or ""
        transformed_value = value
        action: InputTextAction | None = None
        organization_id = context.organization_id
        task_id = context.task_id
        step_id = context.step_id
        workflow_run_id = context.workflow_run_id
        task = await app.DATABASE.get_task(task_id, organization_id) if task_id and organization_id else None
        step = await app.DATABASE.get_step(step_id, organization_id) if step_id and organization_id else None

        if intention:
            try:
                prompt = context.prompt
                data = data or {}
                if value and isinstance(data, dict) and "value" not in data:
                    data["value"] = value

                if (totp_identifier or totp_url) and context and organization_id and task_id:
                    if totp_identifier:
                        totp_identifier = _render_template_with_label(totp_identifier, label=self.current_label)
                    if totp_url:
                        totp_url = _render_template_with_label(totp_url, label=self.current_label)
                    otp_value = await poll_otp_value(
                        organization_id=organization_id,
                        task_id=task_id,
                        workflow_run_id=workflow_run_id,
                        totp_identifier=totp_identifier,
                        totp_verification_url=totp_url,
                    )
                    if otp_value and otp_value.get_otp_type() == OTPType.TOTP:
                        verification_code = otp_value.value
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

                # Try to get element_id from selector if selector is provided
                element_id = await _get_element_id_by_selector(selector, self.page) if selector else None

                if element_id:
                    # The selector/element is valid, using a simpler/smaller prompt
                    script_generation_input_text_prompt = prompt_engine.load_prompt(
                        template="script-generation-input-text-generatiion",
                        intention=intention,
                        goal=prompt,
                        data=data,
                    )
                    json_response = await app.SINGLE_INPUT_AGENT_LLM_API_HANDLER(
                        prompt=script_generation_input_text_prompt,
                        prompt_name="script-generation-input-text-generatiion",
                        step=step,
                        organization_id=organization_id,
                    )
                    value = json_response.get("answer", value)

                    if context and context.workflow_run_id:
                        transformed_value = await _get_actual_value_of_parameter_if_secret(
                            context.workflow_run_id, str(value)
                        )
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
                else:
                    # Use a heavier single-input-action when selector is not found
                    element_tree = refreshed_page.build_element_tree()
                    payload_str = _get_context_data(data)
                    merged_goal = INPUT_GOAL.format(intention=intention, prompt=prompt)

                    single_input_prompt = prompt_engine.load_prompt(
                        template="single-input-action",
                        navigation_goal=merged_goal,
                        navigation_payload_str=payload_str,
                        current_url=self.page.url,
                        elements=element_tree,
                        local_datetime=datetime.now(context.tz_info or datetime.now().astimezone().tzinfo).isoformat(),
                    )
                    json_response = await app.SINGLE_INPUT_AGENT_LLM_API_HANDLER(
                        prompt=single_input_prompt,
                        prompt_name="single-input-action",
                        step=step,
                        organization_id=organization_id,
                    )

                    actions_json = json_response.get("actions", [])
                    if actions_json and task and step:
                        actions = parse_actions(task, step.step_id, step.order, refreshed_page, actions_json)
                        if actions and isinstance(actions[0], InputTextAction):
                            action = cast(InputTextAction, actions[0])
            except Exception:
                LOG.exception(f"Failed to adapt value for input text action on selector={selector}, value={value}")

        if action and organization_id and task and step:
            result = await handle_input_text_action(action, self.page, self.scraped_page, task, step)
            if result and result[-1].success is False:
                raise Exception(result[-1].exception_message)
        else:
            locator = self.page.locator(selector)
            await handler_utils.input_sequentially(locator, transformed_value, timeout=timeout)
        return value

    async def ai_upload_file(
        self,
        selector: str | None,
        files: str | None,
        intention: str,
        data: str | dict[str, Any] | None = None,
        timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS,
        public_url_only: bool = False,
    ) -> str:
        """Upload a file using AI to process the file URL."""

        context = skyvern_context.ensure_context()
        files = files or ""
        action: UploadFileAction | None = None
        organization_id = context.organization_id
        task_id = context.task_id
        step_id = context.step_id
        workflow_run_id = context.workflow_run_id
        task = await app.DATABASE.get_task(task_id, organization_id) if task_id and organization_id else None
        step = await app.DATABASE.get_step(step_id, organization_id) if step_id and organization_id else None

        if intention:
            try:
                prompt = context.prompt
                data = data or {}
                if files and isinstance(data, dict) and "files" not in data:
                    data["files"] = files

                refreshed_page = await self.scraped_page.generate_scraped_page_without_screenshots()
                self.scraped_page = refreshed_page

                # Try to get element_id from selector if selector is provided
                element_id = await _get_element_id_by_selector(selector, self.page) if selector else None

                if element_id:
                    # The selector/element is valid, using a simpler/smaller prompt
                    script_generation_file_url_prompt = prompt_engine.load_prompt(
                        template="script-generation-file-url-generation",
                        intention=intention,
                        data=data,
                        goal=prompt,
                    )
                    json_response = await app.SINGLE_INPUT_AGENT_LLM_API_HANDLER(
                        prompt=script_generation_file_url_prompt,
                        prompt_name="script-generation-file-url-generation",
                        step=step,
                        organization_id=organization_id,
                    )
                    files = json_response.get("answer", files)

                    action = UploadFileAction(
                        element_id=element_id,
                        file_url=files,
                        status=ActionStatus.pending,
                        organization_id=organization_id,
                        workflow_run_id=workflow_run_id,
                        task_id=task_id,
                        step_id=context.step_id if context else None,
                        reasoning=intention,
                        intention=intention,
                        response=files,
                    )
                else:
                    # Use a heavier single-upload-action when selector is not found
                    element_tree = refreshed_page.build_element_tree()
                    payload_str = _get_context_data(data)
                    merged_goal = UPLOAD_GOAL.format(intention=intention, prompt=prompt)

                    single_upload_prompt = prompt_engine.load_prompt(
                        template="single-upload-action",
                        navigation_goal=merged_goal,
                        navigation_payload_str=payload_str,
                        current_url=self.page.url,
                        elements=element_tree,
                        local_datetime=datetime.now(context.tz_info or datetime.now().astimezone().tzinfo).isoformat(),
                    )
                    json_response = await app.SINGLE_INPUT_AGENT_LLM_API_HANDLER(
                        prompt=single_upload_prompt,
                        prompt_name="single-upload-action",
                        step=step,
                        organization_id=organization_id,
                    )

                    actions_json = json_response.get("actions", [])
                    if actions_json and task and step:
                        actions = parse_actions(task, step.step_id, step.order, refreshed_page, actions_json)
                        if actions and isinstance(actions[0], UploadFileAction):
                            action = cast(UploadFileAction, actions[0])
                            files = action.file_url
            except Exception:
                LOG.exception(f"Failed to adapt value for upload file action on selector={selector}, file={files}")

        if public_url_only and not validate_download_url(files):
            raise Exception("Only public URLs are allowed")

        if action and organization_id and task and step:
            result = await handle_upload_file_action(action, self.page, self.scraped_page, task, step)
            if result and result[-1].success is False:
                raise Exception(result[-1].exception_message)

        return files

    async def ai_select_option(
        self,
        selector: str | None,
        value: str | None,
        intention: str,
        data: str | dict[str, Any] | None = None,
        timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS,
    ) -> str:
        """Select an option from a dropdown using AI."""

        option_value = value or ""
        context = skyvern_context.current()
        if context and context.task_id and context.step_id and context.organization_id:
            task = await app.DATABASE.get_task(context.task_id, organization_id=context.organization_id)
            step = await app.DATABASE.get_step(context.step_id, organization_id=context.organization_id)
            if intention and task and step:
                try:
                    prompt = context.prompt if context else None
                    # data = _get_context_data(data)
                    data = data or {}
                    if value and isinstance(data, dict) and "value" not in data:
                        data["value"] = value

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
                        step=step,
                        organization_id=context.organization_id if context else None,
                    )
                    actions = parse_actions(
                        task, step.step_id, step.order, self.scraped_page, json_response.get("actions", [])
                    )
                    if actions:
                        action = cast(SelectOptionAction, actions[0])
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
                            f"Failed to parse actions for select option action on selector={selector}, value={value}"
                        )
                except Exception:
                    LOG.exception(
                        f"Failed to adapt value for select option action on selector={selector}, value={value}"
                    )
        else:
            locator = self.page.locator(selector)
            await locator.select_option(option_value, timeout=timeout)
        return option_value

    async def ai_extract(
        self,
        prompt: str,
        schema: dict[str, Any] | list | str | None = None,
        error_code_mapping: dict[str, str] | None = None,
        intention: str | None = None,
        data: str | dict[str, Any] | None = None,
    ) -> dict[str, Any] | list | str | None:
        """Extract information from the page using AI."""

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
        if context and context.script_mode:
            print(f"\n✨ 📊 Extracted Information:\n{'-' * 50}")

            try:
                # Pretty print JSON if result is a dict/list
                if isinstance(result, (dict, list)):
                    print(json.dumps(result, indent=2, ensure_ascii=False))
                else:
                    print(result)
            except Exception:
                print(result)
            print(f"{'-' * 50}\n")
        return result

    async def ai_act(
        self,
        prompt: str,
    ) -> None:
        """Perform an action on the page using AI based on a natural language prompt."""
        context = skyvern_context.ensure_context()
        organization_id = context.organization_id
        task_id = context.task_id
        step_id = context.step_id

        task = await app.DATABASE.get_task(task_id, organization_id) if task_id and organization_id else None
        step = await app.DATABASE.get_step(step_id, organization_id) if step_id and organization_id else None

        if not task or not step:
            LOG.warning("ai_act: missing task or step", task_id=task_id, step_id=step_id)
            return

        # First, infer the action type from the prompt
        infer_action_type_prompt = prompt_engine.load_prompt(
            template="infer-action-type",
            navigation_goal=prompt,
        )

        json_response = await app.SINGLE_INPUT_AGENT_LLM_API_HANDLER(
            prompt=infer_action_type_prompt,
            prompt_name="infer-action-type",
            step=step,
            organization_id=organization_id,
        )

        if not json_response or "inferred_actions" not in json_response:
            LOG.warning("ai_act: failed to infer action type", prompt=prompt, response=json_response)
            return

        inferred_actions = json_response.get("inferred_actions", [])
        if not inferred_actions:
            error = json_response.get("error")
            LOG.warning("ai_act: no action type inferred", prompt=prompt, error=error)
            return

        action_info = inferred_actions[0]
        action_type = action_info.get("action_type")
        confidence = action_info.get("confidence_float", 0.0)

        LOG.info(
            "ai_act: inferred action type",
            prompt=prompt,
            action_type=action_type,
            confidence=confidence,
            reasoning=action_info.get("reasoning"),
        )

        refreshed_page = await self.scraped_page.generate_scraped_page_without_screenshots()
        self.scraped_page = refreshed_page
        element_tree = refreshed_page.build_element_tree()

        template: str
        llm_handler: Any
        if action_type == "CLICK":
            template = "single-click-action"
            llm_handler = app.SINGLE_CLICK_AGENT_LLM_API_HANDLER
        elif action_type == "INPUT_TEXT":
            template = "single-input-action"
            llm_handler = app.SINGLE_INPUT_AGENT_LLM_API_HANDLER
        elif action_type == "UPLOAD_FILE":
            template = "single-upload-action"
            llm_handler = app.SINGLE_INPUT_AGENT_LLM_API_HANDLER
        elif action_type == "SELECT_OPTION":
            template = "single-select-action"
            llm_handler = app.SELECT_AGENT_LLM_API_HANDLER
        else:
            LOG.warning("ai_act: unknown action type", action_type=action_type, prompt=prompt)
            return

        single_action_prompt = prompt_engine.load_prompt(
            template=template,
            navigation_goal=prompt,
            navigation_payload_str=None,
            current_url=self.page.url,
            elements=element_tree,
            local_datetime=datetime.now(context.tz_info or datetime.now().astimezone().tzinfo).isoformat(),
        )

        try:
            action_response = await llm_handler(
                prompt=single_action_prompt,
                prompt_name=template,
                step=step,
                organization_id=organization_id,
            )

            actions_json = action_response.get("actions", [])
            if not actions_json:
                LOG.warning("ai_act: no actions generated", prompt=prompt, action_type=action_type)
                return

            actions = parse_actions(task, step.step_id, step.order, refreshed_page, actions_json)
            if not actions:
                LOG.warning("ai_act: failed to parse actions", prompt=prompt, action_type=action_type)
                return

            action = actions[0]

            if action_type == "CLICK" and isinstance(action, ClickAction):
                result = await handle_click_action(action, self.page, refreshed_page, task, step)
            elif action_type == "INPUT_TEXT" and isinstance(action, InputTextAction):
                result = await handle_input_text_action(action, self.page, refreshed_page, task, step)
            elif action_type == "UPLOAD_FILE" and isinstance(action, UploadFileAction):
                result = await handle_upload_file_action(action, self.page, refreshed_page, task, step)
            elif action_type == "SELECT_OPTION" and isinstance(action, SelectOptionAction):
                result = await handle_select_option_action(action, self.page, refreshed_page, task, step)
            else:
                LOG.warning(
                    "ai_act: action type mismatch",
                    expected_type=action_type,
                    actual_type=type(action).__name__,
                    prompt=prompt,
                )
                return

            if result and result[-1].success is False:
                raise Exception(result[-1].exception_message)

        except Exception:
            LOG.exception("ai_act: failed to execute action", action_type=action_type, prompt=prompt)


async def _get_actual_value_of_parameter_if_secret(workflow_run_id: str, parameter: str) -> Any:
    """
    Get the actual value of a parameter if it's a secret. If it's not a secret, return the parameter value as is.

    Just return the parameter value if the task isn't a workflow's task.

    This is only used for InputTextAction, UploadFileAction, and ClickAction (if it has a file_url).
    """
    workflow_run_context = app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context(workflow_run_id)
    secret_value = workflow_run_context.get_original_secret_value_or_none(parameter)
    return secret_value if secret_value is not None else parameter
