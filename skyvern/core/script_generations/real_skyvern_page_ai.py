from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from typing import Any, cast, get_args

import structlog
from jinja2.sandbox import SandboxedEnvironment
from playwright.async_api import Page

from skyvern.config import settings
from skyvern.constants import SKYVERN_PAGE_MAX_SCRAPING_RETRIES, SPECIAL_FIELD_VERIFICATION_CODE
from skyvern.core.script_generations.skyvern_page_ai import SYSTEM_PROMPT_UNSET, SkyvernPageAi
from skyvern.exceptions import NoTOTPSecretFound, SkyvernActionFailed, WorkflowRunContextNotInitialized
from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.api.files import validate_download_url
from skyvern.forge.sdk.api.llm.schema_validator import validate_and_fill_extraction_result
from skyvern.forge.sdk.cache import extraction_cache
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.schemas.totp_codes import OTPType
from skyvern.forge.sdk.services.credentials import is_unresolved_totp_value
from skyvern.schemas.workflows import BlockStatus
from skyvern.services import script_service
from skyvern.services.otp_service import poll_otp_value
from skyvern.services.script_reviewer_v3.cohort import is_v3_cohort
from skyvern.services.script_reviewer_v3.midrun import v3_review_in_flight
from skyvern.services.script_reviewer_v3.types import FailureContext, InterceptedActionType
from skyvern.utils.css_selector import compute_selector_options
from skyvern.utils.prompt_engine import load_prompt_with_elements, load_prompt_with_elements_tracked
from skyvern.utils.prompt_truncation import truncate_extraction_schema
from skyvern.webeye.actions import handler_utils
from skyvern.webeye.actions.actions import (
    ActionStatus,
    ClickAction,
    HoverAction,
    InputTextAction,
    SelectOptionAction,
    UploadFileAction,
)
from skyvern.webeye.actions.handler import (
    get_actual_value_of_parameter_if_secret,
    handle_click_action,
    handle_hover_action,
    handle_input_text_action,
    handle_select_option_action,
    handle_upload_file_action,
)
from skyvern.webeye.actions.parse_actions import parse_actions
from skyvern.webeye.scraper.scraped_page import ScrapedPage

jinja_sandbox_env = SandboxedEnvironment()

LOG = structlog.get_logger()

_INTERCEPTED_ACTION_TYPES = frozenset(get_args(InterceptedActionType))

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
        # A selector miss is an expected, handled fallback: callers treat a None return as
        # a signal to take the heavier AI single-action path (and record a fallback episode),
        # so warn without a stack trace rather than logging an error on every stale selector.
        LOG.warning("Failed to get element id by selector", selector=selector)
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

    # Inject loop metadata from script `skyvern.loop()` / `skyvern.while_loop()`
    # (current_value, current_index, current_item) so cached function bodies inside
    # loops can resolve templates. while_loop yields null current_value; index is set.
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
        recoverable_marker_id: int | None = None,
        v3_parent_episode_id: str | None = None,
    ) -> str | None:
        """Click an element using AI to locate it based on intention.

        Args:
            failed_selector: The original CSS selector that failed before falling
                back to AI. Used to record an element-level fallback episode so
                the script reviewer can fix the selector. Only set when called from
                the ai='fallback' path in skyvern_page.py.
            block_label: The cached block label (from SkyvernPage.current_label).
            v3_parent_episode_id: pre-existing episode id from v3 mid-run Class B
                fall-through. When set, this call updates that episode instead of
                creating a duplicate.
        """
        # v3 mid-run hook: only on the original call (not on internal recursion).
        # If v3 commits an in-flight fix (Class A), we short-circuit; if it
        # gives up (Class B), the episode already exists and we propagate
        # v3_parent_episode_id so the agent-fallback path updates it instead
        # of creating a duplicate row.
        if v3_parent_episode_id is None:
            ep_id, class_a = await self._maybe_run_v3_midrun(
                action_type="click",
                failed_selector=failed_selector,
                intention=intention,
                value=None,
                totp_identifier=None,
                totp_url=None,
                block_label=block_label,
            )
            if class_a:
                # v3's live_try_click already advanced the page state.
                return None
            if ep_id is not None:
                v3_parent_episode_id = ep_id

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
                raise SkyvernActionFailed(
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
                    raise SkyvernActionFailed(result[-1].exception_message or "Click action returned success=False")
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
                    recoverable_marker_id=recoverable_marker_id,
                    v3_parent_episode_id=v3_parent_episode_id,
                )

                return selector
        except SkyvernActionFailed:
            if selector is None:
                raise
            LOG.warning(
                "AI click failed, falling back to original selector",
                selector=selector,
                intention=intention,
            )
        except Exception:
            if selector is None:
                raise
            LOG.exception(
                f"Failed to do ai click. Falling back to original selector={selector}, intention={intention}, data={data}"
            )

        if selector:
            locator = self.page.locator(selector)
            await locator.click(timeout=timeout)
            return selector

        raise SkyvernActionFailed(f"AI click failed and no fallback selector available for intention: {intention}")

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
        recoverable_marker_id: int | None = None,
        v3_parent_episode_id: str | None = None,
    ) -> str:
        """Input text into an element using AI to determine the value."""

        # v3 mid-run hook — see ai_click for the contract.
        # Never expose an unresolved TOTP value to a reviewer that can write its prompt value directly to the page.
        if v3_parent_episode_id is None and not is_unresolved_totp_value(value):
            ep_id, class_a = await self._maybe_run_v3_midrun(
                action_type="fill",
                failed_selector=failed_selector,
                intention=intention,
                value=value,
                totp_identifier=totp_identifier,
                totp_url=totp_url,
                block_label=block_label,
            )
            if class_a:
                # v3 mid-run committed the fill on the live page. The
                # caller relies on page state, not this return value; "" means
                # no selector was resolved.
                return ""
            if ep_id is not None:
                v3_parent_episode_id = ep_id

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
                LOG.exception("Failed to adapt value for input text action", selector=selector)

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
                recoverable_marker_id=recoverable_marker_id,
                v3_parent_episode_id=v3_parent_episode_id,
            )
        else:
            if is_unresolved_totp_value(transformed_value):
                raise NoTOTPSecretFound()
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
        # Defaults so the meta write below is always safe — the LLM-exception
        # path skips the assignments inside the try block, but UNKNOWN can
        # still be returned and the metadata fields must be defined.
        reasoning: str = ""
        confidence: float = 0.0
        extracted_text_for_meta: str = ""

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
                        self._store_classify_result(
                            result,
                            current_url=current_url,
                            options=options,
                            block_label=self.current_label,
                        )
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
                    self._store_classify_result(
                        result,
                        current_url=current_url,
                        options=options,
                        block_label=self.current_label,
                    )
                    return result

        # Tier 2: Mini-LLM classification
        if not scraped:
            await self._refresh_scraped_page(take_screenshots=False)
        extracted_text_for_meta = (self.scraped_page.extracted_text or "")[:2000]

        classify_prompt = prompt_engine.load_prompt(
            template="page-classify",
            current_url=current_url,
            extracted_text=extracted_text_for_meta,
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
        self._store_classify_result(
            result,
            current_url=current_url,
            options=options,
            block_label=self.current_label,
            rejection_reasoning=reasoning,
            confidence=confidence,
            text_excerpt=extracted_text_for_meta,
        )
        return result

    async def _maybe_run_v3_midrun(
        self,
        *,
        action_type: str,
        failed_selector: str | None,
        intention: str,
        value: str | None,
        totp_identifier: str | None,
        totp_url: str | None,
        block_label: str | None,
    ) -> tuple[str | None, bool]:
        """v3 mid-run gate. Called at the top of ai_click / ai_input_text.

        Returns ``(parent_episode_id, did_class_a)``.

        - ``parent_episode_id`` is set when a v3 episode was created and v3
          ran. None means v3 didn't fire (not v3 cohort, missing context,
          or pre-conditions not met).
        - ``did_class_a`` is True when v3's agent committed an in-flight fix
          via a successful live_try_*. The caller short-circuits the AI
          fallback in that case — the workflow already advanced.

        Never raises — any internal failure logs and returns ``(None, False)``
        so the caller falls through to the existing AI fallback path.
        """
        if failed_selector is None or not failed_selector.strip():
            return None, False

        episode_id_for_cleanup: str | None = None
        organization_id_for_cleanup: str | None = None
        try:
            context = skyvern_context.current()
            if context is None or not context.workflow_permanent_id or not context.workflow_run_id:
                return None, False
            # organization_id is required for episode FK + cohort lookup.
            # Guard explicitly rather than silently coercing to "".
            if not context.organization_id:
                return None, False
            if (context.code_version or 0) < 2:
                return None, False
            # FailureContext narrows the action_type set; bail if the
            # caller passed something outside the typed set.
            if action_type not in _INTERCEPTED_ACTION_TYPES:
                return None, False
            narrowed_action_type = cast(InterceptedActionType, action_type)

            use_v3 = await is_v3_cohort(
                workflow_permanent_id=context.workflow_permanent_id,
                organization_id=context.organization_id,
                workflow_run_id=context.workflow_run_id,
            )
            if not use_v3:
                return None, False

            # Create the episode now so v3's agent can attach decisions to it.
            episode = await app.DATABASE.scripts.create_fallback_episode(
                organization_id=context.organization_id,
                workflow_permanent_id=context.workflow_permanent_id,
                workflow_run_id=context.workflow_run_id,
                block_label=block_label or self.current_label or "unknown",
                fallback_type="element",
                script_revision_id=context.script_revision_id,
                page_url=self.page.url,
                reviewer_version="v3",
            )
            episode_id_for_cleanup = episode.episode_id
            organization_id_for_cleanup = context.organization_id

            fc = FailureContext(
                failed_selector=failed_selector,
                intention=intention,
                action_type=narrowed_action_type,
                value=value,
                totp_identifier=totp_identifier,
                totp_url=totp_url,
                page=self.page,
                context=context,
                episode_id=episode.episode_id,
            )
            result = await v3_review_in_flight(fc)
            return episode.episode_id, result.decision.is_midrun_class_a()
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.warning(
                "v3 mid-run hook raised — falling through to agent fallback",
                action_type=action_type,
                block_label=block_label,
                exc_info=True,
            )
            if episode_id_for_cleanup is not None and organization_id_for_cleanup is not None:
                try:
                    await app.DATABASE.scripts.delete_fallback_episode(
                        episode_id=episode_id_for_cleanup,
                        organization_id=organization_id_for_cleanup,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Double-fault: v3_review_in_flight raised AND cleanup also
                    # failed. Log at error so the orphan is observable in DD —
                    # post-run v3 will see reviewed=False, reviewer_version="v3"
                    # with no useful agent data.
                    LOG.error(
                        "v3_midrun_orphan_episode_cleanup_failed",
                        episode_id=episode_id_for_cleanup,
                        exc_info=True,
                    )
            return None, False

    async def _record_element_fallback_episode(
        self,
        context: skyvern_context.SkyvernContext,
        action_type: str,
        failed_selector: str | None,
        intention: str,
        action: Any,
        block_label: str | None = None,
        recoverable_marker_id: int | None = None,
        v3_parent_episode_id: str | None = None,
    ) -> None:
        """Record an element-level fallback episode when ai_click/ai_input_text fires
        because a CSS selector failed or was missing. Gated on code_version >= 2.

        Two trigger conditions:
        - `failed_selector` set → fallback path: a tried selector missed.
        - `recoverable_marker_id` set → SKY-9436 escape hatch: generator emitted
          `ai='proactive'` because no semantic selector existed at codegen.
          AI succeeded; capture the element pick so the reviewer can later
          upgrade the call to `selector=, ai='fallback'`.

        ``v3_parent_episode_id`` is set on Class B fall-through from mid-run v3.
        The episode row was already created when v3 fired; we update_ it with
        the agent fallback's action data instead of creating a duplicate.
        """
        if failed_selector is None and recoverable_marker_id is None:
            return
        if (context.code_version or 0) < 2:
            return
        if not context.workflow_run_id or not context.workflow_permanent_id:
            return
        try:
            action_data: dict[str, Any] = {
                "action_type": action_type,
                "intention": intention,
                "failed_selector": failed_selector if failed_selector else "(missing — no selector= argument)",
            }
            if recoverable_marker_id is not None:
                action_data["recoverable_marker_id"] = recoverable_marker_id
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

            if recoverable_marker_id is not None and failed_selector is None:
                error_msg = (
                    f"Proactive recovery on page.{action_type}() (marker={recoverable_marker_id}): "
                    f"generator emitted ai='proactive' (no semantic selector at codegen); "
                    f"AI picked the element. Intention: {intention}"
                )
            else:
                error_msg = (
                    f"Selector {'failed' if failed_selector else 'missing'} on page.{action_type}(), "
                    f"AI fallback succeeded. "
                    f"Original selector: {failed_selector or '(none)'}. "
                    f"Intention: {intention}"
                )
            if v3_parent_episode_id:
                # v3 mid-run Class B fall-through: episode already exists; update
                # it with the agent's action_data + error_message instead of
                # creating a duplicate row.
                await app.DATABASE.scripts.update_fallback_episode(
                    episode_id=v3_parent_episode_id,
                    organization_id=context.organization_id or "",
                    agent_actions=action_data,
                    error_message=error_msg,
                )
                LOG.info(
                    "Updated v3 mid-run episode with agent fallback action data",
                    episode_id=v3_parent_episode_id,
                    block_label=block_label or self.current_label,
                    action_type=action_type,
                )
            else:
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
    def _store_classify_result(
        result: str,
        *,
        current_url: str,
        options: dict[str, str],
        block_label: str | None,
        rejection_reasoning: str = "",
        confidence: float = 0.0,
        text_excerpt: str = "",
    ) -> None:
        """Store classify result + (on UNKNOWN) rejection metadata for the next element_fallback to consume."""
        try:
            ctx = skyvern_context.current()
            if ctx is None:
                return
            ctx.last_classify_result = result
            if result == "UNKNOWN":
                ctx.last_classify_meta = {
                    "result": "UNKNOWN",
                    "url_at_classify": current_url,
                    "block_label_at_classify": block_label,
                    "candidate_options": dict(options),
                    "rejection_reasoning": rejection_reasoning,
                    "confidence": confidence,
                    "text_excerpt": (text_excerpt or "")[:500],
                }
            else:
                ctx.last_classify_meta = None
        except Exception:
            LOG.debug("_store_classify_result: failed to persist classify meta", exc_info=True)

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
        max_steps: int = 5,
        validate_first: bool = False,
    ) -> None:
        """Drive an AI agent from the current page state toward ``navigation_goal``.

        Records one fallback episode per invocation regardless of exit path; pops
        any preceding classify metadata at entry so subsequent fallbacks cannot
        inherit it.

        ``validate_first=True`` re-enables the legacy pre-act validate on
        iteration 0 for defensive callers that may invoke this when the page
        already satisfies the goal; the default (False) skips that validate
        because the documented call site (classify-UNKNOWN / selector-failure
        fallback) cannot be on the success state.
        """
        context = skyvern_context.current()
        if not context or not context.organization_id:
            raise Exception("element_fallback requires an active context with organization_id")

        # Consume-at-entry — exception-safe pop.
        classify_meta: dict[str, Any] | None = context.last_classify_meta
        context.last_classify_meta = None

        LOG.info(
            "page.element_fallback: starting from current page",
            navigation_goal=navigation_goal,
            max_steps=max_steps,
            current_url=self.page.url,
            workflow_run_id=context.workflow_run_id,
        )

        page_url_at_entry = self.page.url
        page_text_at_entry: str | None = None
        try:
            page_text_at_entry = (await self.page.inner_text("body"))[:1500]
        except Exception:
            pass

        steps_taken: list[dict] = []
        completed = False
        exception_summary: str | None = None
        captured_exception: BaseException | None = None

        try:
            for step_num in range(max_steps):
                # Skip iteration-0 validate; opt in via validate_first for defensive callers.
                if step_num > 0 or validate_first:
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

                context.current_step_actions = []
                url_before = self.page.url
                try:
                    await self.ai_act(
                        prompt=f"Take the next action to achieve this goal: {navigation_goal}",
                    )
                    actions_this_step = list(context.current_step_actions or [])
                finally:
                    context.current_step_actions = None
                steps_taken.append(
                    {
                        "step": step_num,
                        "url_before": url_before,
                        "url_after": self.page.url,
                        "actions": actions_this_step[:10],
                    }
                )
            else:
                # All max_steps acts ran without an early break — final validate
                # so max_steps=1 can succeed when the lone act completed the goal.
                if max_steps > 0:
                    is_complete = await self.ai_validate(
                        prompt=f"Has the following goal been achieved? Goal: {navigation_goal}",
                    )
                    if is_complete:
                        LOG.info(
                            "page.element_fallback: goal achieved",
                            step_num=max_steps,
                            navigation_goal=navigation_goal,
                        )
                        completed = True
        except Exception as exc:
            exception_summary = f"{type(exc).__name__}: {exc!s}"[:500]
            captured_exception = exc
        finally:
            await self._persist_classify_unknown_episode(
                context=context,
                classify_meta=classify_meta,
                page_url_at_entry=page_url_at_entry,
                page_text_at_entry=page_text_at_entry,
                steps_taken=steps_taken,
                navigation_goal=navigation_goal,
                completed=completed,
                exception_summary=exception_summary,
            )

        if captured_exception is not None:
            raise captured_exception

        if not completed:
            LOG.warning(
                "page.element_fallback: reached max steps without completing",
                max_steps=max_steps,
                navigation_goal=navigation_goal,
            )
            raise Exception(f"Element fallback did not complete within {max_steps} steps")

    async def _persist_classify_unknown_episode(
        self,
        *,
        context: skyvern_context.SkyvernContext,
        classify_meta: dict[str, Any] | None,
        page_url_at_entry: str,
        page_text_at_entry: str | None,
        steps_taken: list[dict],
        navigation_goal: str,
        completed: bool,
        exception_summary: str | None,
    ) -> None:
        """Record the fallback episode; attach classify_meta only on (URL, block_label) match."""
        if not (context.organization_id and context.workflow_run_id and context.workflow_permanent_id):
            LOG.debug("Skipping fallback episode: missing context identifiers")
            return

        # Single narrow: ``meta`` is the trustworthy classify metadata or None.
        # Carries the URL+block_label predecessor invariant.
        meta: dict[str, Any] | None = None
        if (
            classify_meta is not None
            and classify_meta.get("url_at_classify") == page_url_at_entry
            and classify_meta.get("block_label_at_classify") == self.current_label
        ):
            meta = classify_meta

        classify_result_for_episode: str | None = meta.get("result") if meta is not None else None

        # Flat ``actions`` matches the reviewer template iteration; ``steps`` keeps the per-iteration grouping.
        flat_actions: list[dict[str, Any]] = []
        for step in steps_taken:
            for act in step.get("actions") or []:
                flat_actions.append(act)
        agent_actions_payload: dict[str, Any] = {
            "navigation_goal": navigation_goal,
            "completed": completed,
            "steps_taken": len(steps_taken),
            "actions": flat_actions[:20],
            "steps": steps_taken[:20],
        }
        if exception_summary is not None:
            agent_actions_payload["exception_summary"] = exception_summary
        if meta is not None and meta.get("result") == "UNKNOWN":
            agent_actions_payload["classify_rejection"] = {
                "reasoning": meta.get("rejection_reasoning"),
                "confidence": meta.get("confidence"),
                "candidate_options": meta.get("candidate_options"),
                "text_excerpt": meta.get("text_excerpt"),
            }

        error_message = (
            f"classify returned UNKNOWN, element_fallback goal: {navigation_goal}"
            if classify_result_for_episode == "UNKNOWN"
            else f"element_fallback goal: {navigation_goal}"
        )

        try:
            await app.DATABASE.scripts.create_fallback_episode(
                organization_id=context.organization_id,
                workflow_permanent_id=context.workflow_permanent_id,
                workflow_run_id=context.workflow_run_id,
                block_label=self.current_label or "unknown",
                fallback_type="element",
                script_revision_id=context.script_revision_id,
                error_message=error_message,
                classify_result=classify_result_for_episode,
                page_url=page_url_at_entry,
                page_text_snapshot=page_text_at_entry,
                agent_actions=agent_actions_payload,
            )
        except Exception:
            LOG.debug("Failed to record element fallback episode", exc_info=True)

    async def ai_extract(
        self,
        prompt: str,
        schema: dict[str, Any] | list | str | None = None,
        error_code_mapping: dict[str, str] | None = None,
        intention: str | None = None,
        data: str | dict[str, Any] | None = None,
        skip_refresh: bool = False,
        include_extracted_text: bool = True,
        system_prompt: str | None | Any = SYSTEM_PROMPT_UNSET,
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

        # Resolve the effective workflow_system_prompt for this run. Order:
        #   1. Caller-passed value wins (including None — "block opted out,
        #      send no system prompt").
        #   2. Block-recorded value from ``WorkflowRunContext``, populated by
        #      ``Block._apply_workflow_system_prompt`` in both the agent path
        #      (``format_potential_template_parameters``) and the script path
        #      (``_execute_single_block`` before ``exec``). Using the recorded
        #      value makes the block the single source of truth for the
        #      opt-out + resolved-string decision — script-path extractions
        #      hash to the same cache key and send the same LLM input the
        #      agent path would. A recorded ``None`` is a real opt-out, not a
        #      miss (SKY-9147).
        #   3. Fall back to the run-wide effective prompt for non-block
        #      callers (standalone scripts, sdk routes, etc.) that never set
        #      ``current_label`` and never went through a Block.
        workflow_system_prompt: str | None
        if system_prompt is not SYSTEM_PROMPT_UNSET:
            workflow_system_prompt = cast("str | None", system_prompt)
        else:
            workflow_system_prompt = None
            workflow_run_context_for_prompt = None
            if context and context.workflow_run_id:
                try:
                    workflow_run_context_for_prompt = app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context(
                        context.workflow_run_id
                    )
                except WorkflowRunContextNotInitialized:
                    workflow_run_context_for_prompt = None

            if workflow_run_context_for_prompt is not None:
                recorded, value = workflow_run_context_for_prompt.get_block_workflow_system_prompt(self.current_label)
                if recorded:
                    workflow_system_prompt = value
                else:
                    workflow_system_prompt = workflow_run_context_for_prompt.resolve_effective_workflow_system_prompt()

        # Render the prompt FIRST so the cache key hashes the exact string
        # that will be sent to the LLM (captures economy-tree swaps and 2/3
        # truncation inside load_prompt_with_elements).
        extracted_text_for_prompt = self.scraped_page.extracted_text if include_extracted_text else None
        capped_schema = truncate_extraction_schema(schema)
        # Normalize error_code_mapping to the exact string the prompt will
        # render (None when falsy). Hashing this string below means None and
        # {} collapse to one key since both drop the prompt block.
        error_code_mapping_str = json.dumps(error_code_mapping) if error_code_mapping else None

        # Use the _tracked variant so the cache key below can hash post-ceiling
        # values — when the prompt exceeds the hard ceiling,
        # enforce_prompt_ceiling drops fields to None and two requests that
        # render to the same final LLM prompt must share a cache key.
        extract_information_prompt, post_ceiling_kwargs = load_prompt_with_elements_tracked(
            element_tree_builder=self.scraped_page,
            prompt_engine=prompt_engine,
            template_name="extract-information",
            html_need_skyvern_attrs=False,
            data_extraction_goal=prompt,
            extracted_information_schema=capped_schema,
            current_url=self.scraped_page.url,
            extracted_text=extracted_text_for_prompt,
            error_code_mapping_str=error_code_mapping_str,
            local_datetime=local_datetime_str,
        )

        # Cache extract-information within this script-generation path. The
        # `call_path="script"` discriminator structurally isolates these keys
        # from the agent/handler paths so a script-path hit can never replay
        # an agent-path result (or vice versa), even when all other inputs
        # happen to hash identically (e.g. a goal with no `{{ var }}`
        # substitutions and no nav/prev context).
        #
        # Best-effort per the RFC review — any exception falls through to the
        # full LLM call below. The `try` is narrowed to just compute_cache_key
        # + lookup so a downstream log failure can't re-enter the except block
        # and double-count the call as both a hit/miss and a `lookup_error`
        # in the Datadog miss-reason metric.
        workflow_run_id = context.workflow_run_id if context else None
        cache_key: str | None = None
        lookup_result: extraction_cache.LookupResult | None = None
        try:
            # Use the variant of the element tree that load_prompt_with_elements
            # actually rendered (could be economy or 2/3-truncated under token
            # pressure). Falls back to a fresh HTML build when the prior build
            # used fmt=JSON (field is None in that case). The fallback call
            # mutates `last_used_element_tree{_html}` on self.scraped_page;
            # this is intentional — nothing downstream reads those fields after
            # the cache key is computed.
            # navigation_payload / previous_extracted_information intentionally
            # omitted — ai_extract is the script-generation extract path and
            # doesn't carry navigation context.
            # Hash the post-ceiling values so two requests that differ only in
            # dropped fields (schema/extracted_text on oversized prompts) and
            # render to the same final LLM prompt share a cache key. On the
            # primary path `element_tree` is the sanitized rendered form; the
            # JSON-builder fallback above and every other field hash
            # pre-sanitization, which can cost an extra miss but never a wrong
            # hit (canonicalization doesn't touch backticks).
            cache_key = extraction_cache.compute_cache_key(
                call_path="script",
                element_tree=self.scraped_page.last_used_element_tree_html
                or self.scraped_page.build_element_tree(html_need_skyvern_attrs=False),
                extracted_text=post_ceiling_kwargs["extracted_text"],
                current_url=self.scraped_page.url,
                data_extraction_goal=prompt,
                extracted_information_schema=post_ceiling_kwargs["extracted_information_schema"],
                error_code_mapping=error_code_mapping_str,
                llm_key=None,
                workflow_system_prompt=workflow_system_prompt,
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

        if lookup_result is not None and lookup_result.hit and isinstance(lookup_result.value, (dict, list, str)):
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
            return lookup_result.value
        if lookup_result is not None and lookup_result.hit:
            LOG.warning(
                "ai_extract cache hit returned non-cacheable value type; falling through to LLM",
                workflow_run_id=workflow_run_id,
                cache_key=cache_key,
                value_type=type(lookup_result.value).__name__,
                cache_path="script",
            )
        elif lookup_result is not None:
            LOG.info(
                "ai_extract cache miss",
                sampling=True,
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
            system_prompt=workflow_system_prompt,
        )

        # Validate and fill missing fields based on schema
        if schema:
            result = validate_and_fill_extraction_result(
                extraction_result=result,
                schema=schema,
            )

        # Cache the post-validation result so cache hits return the same
        # schema-validated shape as a fresh LLM call. Accept dict / list / str
        # — the `extract-information` prompt uses `force_dict=False`, so root
        # `type: array` or scalar schemas are valid return shapes.
        if cache_key is not None and isinstance(result, (dict, list, str)):
            try:
                extraction_cache.store(workflow_run_id, cache_key, result)
            except Exception:
                LOG.warning("ai_extract cache store failed; ignoring", exc_info=True)

        if context and context.script_mode:
            LOG.debug("Extracted information", result=result)
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
        elif action_type == "HOVER":
            template = "single-hover-action"
            llm_handler = app.SINGLE_CLICK_AGENT_LLM_API_HANDLER
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
            elif action_type == "HOVER" and isinstance(action, HoverAction):
                result = await handle_hover_action(action, self.page, self.scraped_page, task, step)
            else:
                LOG.warning(
                    "ai_act: action type mismatch",
                    expected_type=action_type,
                    actual_type=type(action).__name__,
                    prompt=prompt,
                )
                return

            # ``current_step_actions`` is None outside element_fallback frames; field names match the reviewer template.
            if context.current_step_actions is not None:
                # ai_act treats ``result is None or empty`` as success (no
                # exception raised below). Label "failed" only when there's
                # positive evidence of failure — otherwise "success" — to
                # match the existing ``if result and result[-1].success is
                # False: raise`` semantics two lines below.
                action_failed = bool(result) and result[-1].success is False
                action_record: dict[str, Any] = {
                    "action_type": action_type,
                    "intention": getattr(action, "intention", None),
                    "reasoning": getattr(action, "reasoning", None),
                    "page_url": self.page.url,
                    "status": "failed" if action_failed else "success",
                }
                el_data = getattr(action, "skyvern_element_data", None)
                if el_data:
                    action_record["element_text"] = (el_data.get("text") or "")[:200]
                    action_record["element_tag"] = el_data.get("tagName", "")
                    sel_options = compute_selector_options(el_data)
                    if sel_options:
                        action_record["selector_options"] = sel_options
                        action_record["css_suggestion"] = sel_options[0][0]
                    el_attrs = el_data.get("attributes") or {}
                    if el_attrs:
                        action_record["all_attributes"] = {
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
                if hasattr(action, "get_xpath"):
                    try:
                        xpath = action.get_xpath()
                    except Exception:
                        xpath = None
                    if xpath:
                        action_record["xpath"] = xpath
                context.current_step_actions.append(action_record)

            if result and result[-1].success is False:
                raise Exception(result[-1].exception_message)

        except Exception:
            LOG.exception("ai_act: failed to execute action", action_type=action_type, prompt=prompt)
