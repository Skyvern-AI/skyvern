from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, cast

import structlog
from jinja2.sandbox import SandboxedEnvironment
from playwright.async_api import Page

from skyvern.config import settings
from skyvern.constants import SKYVERN_PAGE_MAX_SCRAPING_RETRIES, SPECIAL_FIELD_VERIFICATION_CODE
from skyvern.core.script_generations.skyvern_page_ai import SkyvernPageAi
from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.api.files import validate_download_url
from skyvern.forge.sdk.api.llm.schema_validator import validate_and_fill_extraction_result
from skyvern.forge.sdk.cache import extraction_cache
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.schemas.totp_codes import OTPType
from skyvern.schemas.workflows import BlockStatus
from skyvern.services import script_service
from skyvern.services.otp_service import poll_otp_value
from skyvern.utils.css_selector import compute_selector_options
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
    get_actual_value_of_parameter_if_secret,
    handle_click_action,
    handle_input_text_action,
    handle_select_option_action,
    handle_upload_file_action,
)
from skyvern.webeye.actions.parse_actions import parse_actions
from skyvern.webeye.scraper.scraped_page import ScrapedPage

jinja_sandbox_env = SandboxedEnvironment()

LOG = structlog.get_logger()

INPUT_GOAL = """- The intention to fill out an input: {intention}.
- The overall goal that the user wants to achieve: {prompt}."""

SELECT_OPTION_GOAL = """- The intention to select an option: {intention}.
- The overall goal that the user wants to achieve: {prompt}."""

UPLOAD_GOAL = """- The intention to upload a file: {intention}.
- The overall goal that the user wants to achieve: {prompt}."""


async def _get_element_id_by_selector(selector: str, page: Page) -> str | None:
    try:
        locator = page.locator(selector)
        element_id = await locator.get_attribute("unique_id", timeout=settings.BROWSER_ACTION_TIMEOUT_MS)
    except Exception:
        LOG.exception("Failed to get element id by selector", selector=selector)
        return None
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

    # Inject for_loop metadata (current_value, current_index, current_item) so
    # that cached function bodies inside for_loops can resolve {{ current_value }}
    # in page.goto() and other template-rendered calls.
    if context and context.loop_metadata:
        for key in ("current_value", "current_index", "current_item"):
            if key in context.loop_metadata:
                template_data[key] = context.loop_metadata[key]

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

    async def _refresh_scraped_page(
        self, take_screenshots: bool = True, max_retries: int = SKYVERN_PAGE_MAX_SCRAPING_RETRIES
    ) -> None:
        self.scraped_page = await self.scraped_page.generate_scraped_page(
            take_screenshots=take_screenshots, max_retries=max_retries
        )

    async def ai_click(
        self,
        selector: str | None,
        intention: str,
        data: str | dict[str, Any] | None = None,
        timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS,
        failed_selector: str | None = None,
        block_label: str | None = None,
    ) -> str | None:
        """Click an element using AI to locate it based on intention.

        Args:
            failed_selector: The original CSS selector that failed before falling
                back to AI. Used to record an element-level fallback episode so
                the script reviewer can fix the selector. Only set when called from
                the ai='fallback' path in skyvern_page.py.
            block_label: The cached block label (from SkyvernPage.current_label).
        """
        try:
            # Build the element tree of the current page for the prompt
            context = skyvern_context.ensure_context()
            payload_str = _get_context_data(data)
            await self._refresh_scraped_page(take_screenshots=False)
            element_tree = self.scraped_page.build_element_tree()

            organization_id = context.organization_id if context else None
            step_id = context.step_id if context else None
            step = await app.DATABASE.tasks.get_step(step_id, organization_id) if step_id and organization_id else None
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
            if not actions_json:
                # LLM returned no actions - the element likely doesn't exist on the page.
                # Raise an exception so the caller knows the action failed.
                raise Exception(
                    f"AI could not find an element to click for intention: {intention}. "
                    "The element may not exist on the current page."
                )
            task_id = context.task_id if context else None
            task = await app.DATABASE.tasks.get_task(task_id, organization_id) if task_id and organization_id else None
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

                # Record element-level fallback episode for the script reviewer (code_v2 only).
                # This fires when a cached script's selector failed (or was missing) and
                # ai_click succeeded. The episode gives the reviewer the AI-found action data
                # so it can write a proper selector.
                await self._record_element_fallback_episode(
                    context=context,
                    action_type="click",
                    failed_selector=failed_selector,
                    intention=intention,
                    action=action,
                    block_label=block_label,
                )

                return selector
        except Exception:
            LOG.exception(
                f"Failed to do ai click. Falling back to original selector={selector}, intention={intention}, data={data}"
            )

        if selector:
            locator = self.page.locator(selector)
            await locator.click(timeout=timeout)
            return selector

        # If we reach here with no selector, the AI failed and there's no fallback - raise an error
        raise Exception(f"AI click failed and no fallback selector available for intention: {intention}")

    async def ai_input_text(
        self,
        selector: str | None,
        value: str | None,
        intention: str,
        data: str | dict[str, Any] | None = None,
        totp_identifier: str | None = None,
        totp_url: str | None = None,
        timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS,
        failed_selector: str | None = None,
        block_label: str | None = None,
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
        task = await app.DATABASE.tasks.get_task(task_id, organization_id) if task_id and organization_id else None
        step = await app.DATABASE.tasks.get_step(step_id, organization_id) if step_id and organization_id else None

        if intention:
            try:
                prompt = context.prompt
                # Merge script_run_parameters into LLM payload (consistency with ai_click/ai_upload_file).
                # No-op when script_run_parameters is unset (i.e. non-adaptive-caching runs).
                data = _get_context_data(data)
                data = data or {}
                if value and isinstance(data, dict) and "value" not in data:
                    data["value"] = value

                otp_value = None
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

                await self._refresh_scraped_page(take_screenshots=False)

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
                        transformed_value = get_actual_value_of_parameter_if_secret(context.workflow_run_id, str(value))
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
                    element_tree = self.scraped_page.build_element_tree()
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
                        actions = parse_actions(task, step.step_id, step.order, self.scraped_page, actions_json)
                        if actions and isinstance(actions[0], InputTextAction):
                            action = cast(InputTextAction, actions[0])
            except Exception:
                LOG.exception(f"Failed to adapt value for input text action on selector={selector}, value={value}")

        if action and organization_id and task and step:
            result = await handle_input_text_action(action, self.page, self.scraped_page, task, step)
            if result and result[-1].success is False:
                raise Exception(result[-1].exception_message)
            await self._record_element_fallback_episode(
                context=context,
                action_type="fill",
                failed_selector=failed_selector,
                intention=intention,
                action=action,
                block_label=block_label,
            )
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
        original_files = files
        action: UploadFileAction | None = None
        organization_id = context.organization_id
        task_id = context.task_id
        step_id = context.step_id
        workflow_run_id = context.workflow_run_id
        task = await app.DATABASE.tasks.get_task(task_id, organization_id) if task_id and organization_id else None
        step = await app.DATABASE.tasks.get_step(step_id, organization_id) if step_id and organization_id else None

        if intention:
            try:
                prompt = context.prompt
                data = data or {}
                if files and isinstance(data, dict) and "files" not in data:
                    data["files"] = files

                await self._refresh_scraped_page(take_screenshots=False)

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
                    element_tree = self.scraped_page.build_element_tree()
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
                        actions = parse_actions(task, step.step_id, step.order, self.scraped_page, actions_json)
                        if actions and isinstance(actions[0], UploadFileAction):
                            action = cast(UploadFileAction, actions[0])
                            files = action.file_url
            except Exception:
                LOG.exception(f"Failed to adapt value for upload file action on selector={selector}, file={files}")

        if action and original_files and action.file_url and action.file_url != original_files:
            LOG.warning(
                "LLM returned a different file url than the user provided, using the original",
                llm_file_url=action.file_url[:20],
                original_file_url=original_files[:20],
            )
            action.file_url = original_files
            files = original_files

        if public_url_only and not validate_download_url(files, organization_id=organization_id):
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
            task = await app.DATABASE.tasks.get_task(context.task_id, organization_id=context.organization_id)
            step = await app.DATABASE.tasks.get_step(context.step_id, organization_id=context.organization_id)
            if intention and task and step:
                try:
                    prompt = context.prompt if context else None
                    # Merge script_run_parameters into LLM payload (consistency with ai_click/ai_upload_file).
                    # No-op when script_run_parameters is unset (i.e. non-adaptive-caching runs).
                    data = _get_context_data(data)
                    data = data or {}
                    if value and isinstance(data, dict) and "value" not in data:
                        data["value"] = value

                    await self._refresh_scraped_page(take_screenshots=False)
                    element_tree = self.scraped_page.build_element_tree()
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

    async def ai_classify(
        self,
        options: dict[str, str],
        url_patterns: dict[str, str] | None = None,
        text_patterns: dict[str, str | list[str]] | None = None,
    ) -> str:
        """Classify the current page state using a tiered cascade.

        Tier 0: URL regex matching (FREE)
        Tier 1: Text substring check in extracted text (FREE, requires scrape)
        Tier 2: Mini-LLM classification (~$0.001)

        Returns the matching option key or "UNKNOWN".
        Also records a branch hit for TTL-based branch pruning.
        """
        current_url = self.page.url
        result = "UNKNOWN"

        # Tier 0: URL pattern matching (FREE)
        if url_patterns:
            for key, pattern in url_patterns.items():
                try:
                    if key in options and re.search(pattern, current_url):
                        LOG.info(
                            "page.classify: matched via URL pattern",
                            key=key,
                            pattern=pattern,
                            url=current_url,
                        )
                        result = key
                        await self._record_branch_hit(result)
                        self._store_classify_result(result)
                        return result
                except re.error:
                    LOG.warning("page.classify: invalid URL regex pattern", key=key, pattern=pattern)

        # Tier 1: Text presence check (FREE, requires scrape)
        scraped = False
        if text_patterns:
            await self._refresh_scraped_page(take_screenshots=False)
            scraped = True
            extracted_text = self.scraped_page.extracted_text or ""
            extracted_lower = extracted_text.lower()
            for key, text_pattern in text_patterns.items():
                if key not in options:
                    continue
                # Accept both a single string and a list of strings
                patterns = text_pattern if isinstance(text_pattern, list) else [text_pattern]
                if all(p.lower() in extracted_lower for p in patterns):
                    LOG.info(
                        "page.classify: matched via text pattern",
                        key=key,
                        pattern=text_pattern,
                    )
                    result = key
                    await self._record_branch_hit(result)
                    self._store_classify_result(result)
                    return result

        # Tier 2: Mini-LLM classification
        if not scraped:
            await self._refresh_scraped_page(take_screenshots=False)
        extracted_text = (self.scraped_page.extracted_text or "")[:2000]

        classify_prompt = prompt_engine.load_prompt(
            template="page-classify",
            current_url=current_url,
            extracted_text=extracted_text,
            options=options,
        )

        context = skyvern_context.current()
        step = None
        organization_id = context.organization_id if context else None
        if context and context.organization_id and context.step_id:
            step = await app.DATABASE.tasks.get_step(
                step_id=context.step_id,
                organization_id=context.organization_id,
            )

        try:
            json_response = await app.SECONDARY_LLM_API_HANDLER(
                prompt=classify_prompt,
                prompt_name="page-classify",
                step=step,
                organization_id=organization_id,
            )
            classification = json_response.get("classification", "UNKNOWN")
            confidence = json_response.get("confidence", 0.0)
            reasoning = json_response.get("reasoning", "")

            LOG.info(
                "page.classify: LLM classification result",
                classification=classification,
                confidence=confidence,
                reasoning=reasoning,
            )

            if classification in options or classification == "UNKNOWN":
                result = classification
            else:
                result = "UNKNOWN"
        except Exception:
            LOG.exception("page.classify: LLM classification failed")
            result = "UNKNOWN"

        await self._record_branch_hit(result)
        self._store_classify_result(result)
        return result

    async def _record_element_fallback_episode(
        self,
        context: skyvern_context.SkyvernContext,
        action_type: str,
        failed_selector: str | None,
        intention: str,
        action: Any,
        block_label: str | None = None,
    ) -> None:
        """Record an element-level fallback episode when ai_click/ai_input_text fires
        because a CSS selector failed or was missing. Gated on code_version >= 2.

        This gives the script reviewer the signal AND the action data (css_suggestion,
        element attributes) it needs to write a proper selector for the next script version.
        """
        if failed_selector is None:
            # None means the caller didn't pass failed_selector — this is a direct
            # ai_click call (not from the ai='fallback' path), so don't record.
            return
        if (context.code_version or 0) < 2:
            return
        if not context.workflow_run_id or not context.workflow_permanent_id:
            return
        try:
            # Build agent_actions data for the reviewer
            action_data: dict[str, Any] = {
                "action_type": action_type,
                "intention": intention,
                "failed_selector": failed_selector if failed_selector else "(missing — no selector= argument)",
            }
            if hasattr(action, "element_id"):
                action_data["element_id"] = action.element_id
            if hasattr(action, "skyvern_element_data") and action.skyvern_element_data:
                el_data = action.skyvern_element_data
                el_attrs = el_data.get("attributes") or {}
                action_data["element_tag"] = el_data.get("tagName", "")
                action_data["element_text"] = (el_data.get("text") or "")[:200]
                action_data["all_attributes"] = {
                    k: v
                    for k, v in el_attrs.items()
                    if k
                    in (
                        "id",
                        "name",
                        "class",
                        "aria-label",
                        "placeholder",
                        "type",
                        "role",
                        "data-testid",
                        "href",
                        "value",
                        "title",
                    )
                }
                # el_data already has the shape compute_selector_options expects
                # (tagName, attributes dict, text) — pass it directly.
                sel_options = compute_selector_options(el_data)
                if sel_options:
                    action_data["css_suggestion"] = sel_options[0][0]
                    action_data["selector_options"] = sel_options
                else:
                    # Fall back to xpath only if no CSS selector can be derived
                    xpath = action.get_xpath() if hasattr(action, "get_xpath") else None
                    if xpath:
                        action_data["css_suggestion"] = f"xpath={xpath}"
            if hasattr(action, "reasoning"):
                action_data["reasoning"] = action.reasoning

            error_msg = (
                f"Selector {'failed' if failed_selector else 'missing'} on page.{action_type}(), "
                f"AI fallback succeeded. "
                f"Original selector: {failed_selector or '(none)'}. "
                f"Intention: {intention}"
            )
            await app.DATABASE.scripts.create_fallback_episode(
                organization_id=context.organization_id or "",
                workflow_permanent_id=context.workflow_permanent_id,
                workflow_run_id=context.workflow_run_id,
                block_label=block_label or self.current_label or "unknown",
                fallback_type="element",
                script_revision_id=context.script_revision_id,
                error_message=error_msg,
                page_url=self.page.url,
                agent_actions=action_data,
            )
            LOG.info(
                "Recorded element fallback episode for selector failure",
                block_label=block_label or self.current_label,
                action_type=action_type,
                failed_selector=failed_selector,
            )
        except Exception:
            LOG.warning("Failed to record element fallback episode for selector failure", exc_info=True)

    @staticmethod
    def _store_classify_result(result: str) -> None:
        """Store the classify result on SkyvernContext for fallback episode recording."""
        try:
            ctx = skyvern_context.current()
            if ctx:
                ctx.last_classify_result = result
        except Exception:
            pass

    async def _record_branch_hit(self, branch_key: str) -> None:
        """Best-effort recording of a classify branch hit for TTL tracking."""
        try:
            context = skyvern_context.current()
            if not context or not context.organization_id or not context.workflow_permanent_id:
                return
            block_label = self.current_label or "unknown"
            await app.DATABASE.scripts.record_branch_hit(
                organization_id=context.organization_id,
                workflow_permanent_id=context.workflow_permanent_id,
                block_label=block_label,
                branch_key=branch_key,
            )
        except Exception:
            LOG.debug("Failed to record branch hit", exc_info=True)

    async def ai_element_fallback(
        self,
        navigation_goal: str,
        max_steps: int = 10,
    ) -> None:
        """Activate the AI agent from the CURRENT page position to achieve a navigation goal.

        Uses repeated ai_act calls with ai_validate checks to execute from the
        current page state. Records all actions taken for later review by the
        AI Script Reviewer.
        """
        context = skyvern_context.current()
        if not context or not context.organization_id:
            raise Exception("element_fallback requires an active context with organization_id")

        LOG.info(
            "page.element_fallback: starting from current page",
            navigation_goal=navigation_goal,
            max_steps=max_steps,
            current_url=self.page.url,
            workflow_run_id=context.workflow_run_id,
        )

        # Capture page state before element fallback for episode recording
        page_url_at_entry = self.page.url
        page_text_at_entry: str | None = None
        try:
            page_text_at_entry = (await self.page.inner_text("body"))[:1500]
        except Exception:
            pass

        steps_taken: list[dict] = []
        completed = False

        for step_num in range(max_steps):
            # Check if the goal has been achieved
            is_complete = await self.ai_validate(
                prompt=f"Has the following goal been achieved? Goal: {navigation_goal}",
            )
            if is_complete:
                LOG.info(
                    "page.element_fallback: goal achieved",
                    step_num=step_num,
                    navigation_goal=navigation_goal,
                )
                completed = True
                break

            LOG.info(
                "page.element_fallback: executing step",
                step_num=step_num,
                current_url=self.page.url,
            )

            # Let the AI agent take an action toward the goal
            await self.ai_act(
                prompt=f"Take the next action to achieve this goal: {navigation_goal}",
            )
            steps_taken.append({"step": step_num, "url_after": self.page.url})

        # Record an element fallback episode for the feedback loop
        if context.workflow_run_id and context.workflow_permanent_id:
            try:
                await app.DATABASE.scripts.create_fallback_episode(
                    organization_id=context.organization_id,
                    workflow_permanent_id=context.workflow_permanent_id,
                    workflow_run_id=context.workflow_run_id,
                    block_label=self.current_label or "unknown",
                    fallback_type="element",
                    script_revision_id=context.script_revision_id,
                    error_message=f"classify returned UNKNOWN, element_fallback goal: {navigation_goal}",
                    page_url=page_url_at_entry,
                    page_text_snapshot=page_text_at_entry,
                    agent_actions={
                        "navigation_goal": navigation_goal,
                        "completed": completed,
                        "steps_taken": len(steps_taken),
                        "steps": steps_taken[:20],
                    },
                )
            except Exception:
                LOG.debug("Failed to record element fallback episode", exc_info=True)

        if not completed:
            LOG.warning(
                "page.element_fallback: reached max steps without completing",
                max_steps=max_steps,
                navigation_goal=navigation_goal,
            )
            raise Exception(f"Element fallback did not complete within {max_steps} steps")

    async def ai_extract(
        self,
        prompt: str,
        schema: dict[str, Any] | list | str | None = None,
        error_code_mapping: dict[str, str] | None = None,
        intention: str | None = None,
        data: str | dict[str, Any] | None = None,
        skip_refresh: bool = False,
    ) -> dict[str, Any] | list | str | None:
        """Extract information from the page using AI."""

        if not skip_refresh:
            await self._refresh_scraped_page(take_screenshots=True)
        context = skyvern_context.current()
        tz_info = datetime.now(tz=timezone.utc).tzinfo
        if context and context.tz_info:
            tz_info = context.tz_info
        prompt = _render_template_with_label(prompt, label=self.current_label)
        local_datetime_str = datetime.now(tz_info).isoformat()

        # Render the prompt FIRST so the cache key hashes the exact string
        # that will be sent to the LLM (captures economy-tree swaps and 2/3
        # truncation inside load_prompt_with_elements).
        extract_information_prompt = load_prompt_with_elements(
            element_tree_builder=self.scraped_page,
            prompt_engine=prompt_engine,
            template_name="extract-information",
            html_need_skyvern_attrs=False,
            data_extraction_goal=prompt,
            extracted_information_schema=schema,
            current_url=self.scraped_page.url,
            extracted_text=self.scraped_page.extracted_text,
            error_code_mapping_str=(json.dumps(error_code_mapping) if error_code_mapping else None),
            local_datetime=local_datetime_str,
        )

        # Share the extract-information cache with the agent path. Best-effort
        # per the RFC review — any exception falls through to the full LLM
        # call below. The `try` is narrowed to just compute_cache_key + lookup
        # so a downstream log failure can't re-enter the except block and
        # double-count the call as both a hit/miss and a `lookup_error` in
        # the Datadog miss-reason metric.
        workflow_run_id = context.workflow_run_id if context else None
        cache_key: str | None = None
        lookup_result: extraction_cache.LookupResult | None = None
        try:
            cache_key = extraction_cache.compute_cache_key(
                rendered_prompt=extract_information_prompt,
                llm_key=None,
            )
            lookup_result = extraction_cache.lookup(workflow_run_id, cache_key)
        except Exception:
            LOG.warning(
                "ai_extract cache lookup failed; falling through to LLM",
                workflow_run_id=workflow_run_id,
                cache_key=cache_key,
                cache_hit=False,
                cache_scope=extraction_cache.SCOPE_RUN,
                cache_age_seconds=None,
                fallback_reason=extraction_cache.FALLBACK_LOOKUP_ERROR,
                cache_path="script",
                exc_info=True,
            )
            # Preserve cache_key so the downstream store() can still warm the cache
            # for subsequent identical calls even when lookup() fails transiently.

        if lookup_result is not None and lookup_result.hit:
            LOG.info(
                "ai_extract cache hit — skipping LLM call",
                workflow_run_id=workflow_run_id,
                cache_key=cache_key,
                cache_hit=True,
                cache_scope=lookup_result.scope,
                cache_age_seconds=lookup_result.age_seconds,
                fallback_reason=None,
                cache_path="script",
            )
            return lookup_result.value  # type: ignore[return-value]
        if lookup_result is not None:
            LOG.info(
                "ai_extract cache miss",
                workflow_run_id=workflow_run_id,
                cache_key=cache_key,
                cache_hit=False,
                cache_scope=lookup_result.scope,
                cache_age_seconds=None,
                fallback_reason=lookup_result.fallback_reason,
                cache_path="script",
            )
        step = None
        if context and context.organization_id and context.task_id and context.step_id:
            step = await app.DATABASE.tasks.get_step(
                step_id=context.step_id,
                organization_id=context.organization_id,
            )

        result = await app.EXTRACTION_LLM_API_HANDLER(
            prompt=extract_information_prompt,
            step=step,
            screenshots=self.scraped_page.screenshots,
            prompt_name="extract-information",
            force_dict=False,
        )

        # Validate and fill missing fields based on schema
        if schema:
            result = validate_and_fill_extraction_result(
                extraction_result=result,
                schema=schema,
            )

        # Cache the post-validation result so cache hits return the same
        # schema-validated shape as a fresh LLM call.
        if cache_key is not None and result is not None:
            try:
                extraction_cache.store(workflow_run_id, cache_key, result)
            except Exception:
                LOG.warning("ai_extract cache store failed; ignoring", exc_info=True)

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

    async def ai_validate(
        self,
        prompt: str,
        model: dict[str, Any] | None = None,
    ) -> bool:
        result = await script_service.execute_validation(
            complete_criterion=prompt,
            terminate_criterion=None,
            error_code_mapping=None,
            model=model,
        )
        return result.status == BlockStatus.completed

    async def ai_locate_element(
        self,
        prompt: str,
    ) -> str | None:
        """Locate an element on the page using AI and return its XPath selector.

        Args:
            prompt: Natural language description of the element to locate (e.g., 'find "download invoices" button')

        Returns:
            XPath selector string (e.g., 'xpath=//button[@id="download"]') or None if not found
        """
        scraped_page_refreshed = await self.scraped_page.refresh()
        context = skyvern_context.ensure_context()

        prompt_rendered = _render_template_with_label(prompt, label=self.current_label)

        locate_element_prompt = load_prompt_with_elements(
            element_tree_builder=scraped_page_refreshed,
            prompt_engine=prompt_engine,
            template_name="single-locate-element",
            html_need_skyvern_attrs=True,
            data_extraction_goal=prompt_rendered,
            current_url=scraped_page_refreshed.url,
            local_datetime=datetime.now(context.tz_info or datetime.now().astimezone().tzinfo).isoformat(),
        )

        step = None
        if context.organization_id and context.task_id and context.step_id:
            step = await app.DATABASE.tasks.get_step(
                step_id=context.step_id,
                organization_id=context.organization_id,
            )

        result = await app.EXTRACTION_LLM_API_HANDLER(
            prompt=locate_element_prompt,
            step=step,
            screenshots=scraped_page_refreshed.screenshots,
            prompt_name="single-locate-element",
        )

        if not result or not isinstance(result, dict):
            LOG.error(
                "AI locate element failed - invalid result",
                result=result,
                result_type=type(result).__name__,
                prompt=prompt_rendered,
            )
            return None

        element_id = result.get("element_id", None)
        confidence = result.get("confidence_float", 0.0)

        xpath: str | None = None
        if element_id:
            skyvern_element_data = scraped_page_refreshed.id_to_element_dict.get(element_id)
            if skyvern_element_data and "xpath" in skyvern_element_data:
                xpath = skyvern_element_data.get("xpath")

        if not xpath:
            xpath = result.get("xpath", None)

        if not xpath:
            LOG.error(
                "AI locate element failed - no xpath in element data",
                element_id=element_id,
                result=result,
                prompt=prompt_rendered,
            )
            return None

        LOG.info(
            "AI locate element result",
            element_id=element_id,
            xpath=xpath,
            confidence=confidence,
            prompt=prompt_rendered,
        )

        return xpath

    async def ai_prompt(
        self,
        prompt: str,
        schema: dict[str, Any] | None = None,
        model: dict[str, Any] | None = None,
    ) -> dict[str, Any] | list | str | None:
        """Send a prompt to the LLM and get a response based on the provided schema."""
        result = await script_service.prompt(
            prompt=prompt,
            schema=schema,
            model=model,
        )
        return result

    async def ai_act(
        self,
        prompt: str,
        skip_refresh: bool = False,
        use_economy_tree: bool = False,
    ) -> None:
        """Perform an action on the page using AI based on a natural language prompt."""
        context = skyvern_context.ensure_context()
        organization_id = context.organization_id
        task_id = context.task_id
        step_id = context.step_id

        task = await app.DATABASE.tasks.get_task(task_id, organization_id) if task_id and organization_id else None
        step = await app.DATABASE.tasks.get_step(step_id, organization_id) if step_id and organization_id else None

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

        if not skip_refresh:
            await self._refresh_scraped_page(take_screenshots=False)
        if use_economy_tree and self.scraped_page.support_economy_elements_tree():
            element_tree = self.scraped_page.build_economy_elements_tree()
        else:
            element_tree = self.scraped_page.build_element_tree()

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

        local_datetime = datetime.now(context.tz_info or datetime.now().astimezone().tzinfo).isoformat()
        single_action_prompt = prompt_engine.load_prompt(
            template=template,
            navigation_goal=prompt,
            navigation_payload_str=None,
            current_url=self.page.url,
            elements=element_tree,
            local_datetime=local_datetime,
        )

        try:
            action_response = await llm_handler(
                prompt=single_action_prompt,
                prompt_name=template,
                step=step,
                organization_id=organization_id,
            )

            actions_json = action_response.get("actions", [])
            if not actions_json and use_economy_tree:
                LOG.info(
                    "ai_act: economy tree returned no actions, retrying with full tree",
                    prompt=prompt,
                    action_type=action_type,
                )
                await self._refresh_scraped_page(take_screenshots=False)
                element_tree = self.scraped_page.build_element_tree()
                single_action_prompt = prompt_engine.load_prompt(
                    template=template,
                    navigation_goal=prompt,
                    navigation_payload_str=None,
                    current_url=self.page.url,
                    elements=element_tree,
                    local_datetime=local_datetime,
                )
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

            actions = parse_actions(task, step.step_id, step.order, self.scraped_page, actions_json)
            if not actions:
                LOG.warning("ai_act: failed to parse actions", prompt=prompt, action_type=action_type)
                return

            action = actions[0]

            if action_type == "CLICK" and isinstance(action, ClickAction):
                result = await handle_click_action(action, self.page, self.scraped_page, task, step)
            elif action_type == "INPUT_TEXT" and isinstance(action, InputTextAction):
                result = await handle_input_text_action(action, self.page, self.scraped_page, task, step)
            elif action_type == "UPLOAD_FILE" and isinstance(action, UploadFileAction):
                result = await handle_upload_file_action(action, self.page, self.scraped_page, task, step)
            elif action_type == "SELECT_OPTION" and isinstance(action, SelectOptionAction):
                result = await handle_select_option_action(action, self.page, self.scraped_page, task, step)
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
