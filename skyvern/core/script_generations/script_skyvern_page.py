from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Callable

import structlog
from playwright.async_api import Page

from skyvern.config import settings
from skyvern.constants import BROWSER_DOWNLOAD_TIMEOUT
from skyvern.core.script_generations.real_skyvern_page_ai import RealSkyvernPageAi, render_template
from skyvern.core.script_generations.skyvern_page import ActionCall, ActionMetadata, RunContext, SkyvernPage
from skyvern.core.script_generations.skyvern_page_ai import SkyvernPageAi
from skyvern.errors.errors import UserDefinedError
from skyvern.exceptions import ScriptTerminationException, WorkflowRunNotFound
from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.api.files import (
    check_downloading_files_and_wait_for_download_to_complete,
    get_path_for_workflow_download_directory,
    list_files_in_directory,
)
from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.core import skyvern_context
from skyvern.schemas.steps import AgentStepOutput
from skyvern.services.otp_service import poll_otp_value
from skyvern.utils.url_validators import prepend_scheme_and_validate_url
from skyvern.webeye.actions.action_types import ActionType
from skyvern.webeye.actions.actions import (
    Action,
    ActionStatus,
    CompleteAction,
    DecisiveAction,
    ExtractAction,
    SelectOption,
    SolveCaptchaAction,
)
from skyvern.webeye.actions.handler import (
    ActionHandler,
    generate_totp_value,
    get_actual_value_of_parameter_if_secret,
    handle_complete_action,
)
from skyvern.webeye.actions.responses import ActionFailure, ActionResult, ActionSuccess
from skyvern.webeye.browser_state import BrowserState
from skyvern.webeye.scraper.scraped_page import ScrapedPage

LOG = structlog.get_logger()

action_wrap = SkyvernPage.action_wrap


class ScriptSkyvernPage(SkyvernPage):
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
        ai: SkyvernPageAi,
        *,
        recorder: Callable[[ActionCall], None] | None = None,
    ) -> None:
        super().__init__(page=page, ai=ai)
        self.scraped_page = scraped_page
        self._record = recorder or (lambda ac: None)

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
                    browser_profile_id=workflow_run.browser_profile_id,
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
    ) -> ScriptSkyvernPage:
        scraped_page = await cls.create_scraped_page(browser_session_id=browser_session_id)
        page = await scraped_page._browser_state.must_get_working_page()
        ai = RealSkyvernPageAi(scraped_page, page)
        return cls(scraped_page=scraped_page, page=page, ai=ai)

    @classmethod
    async def create_scraped_page(
        cls,
        browser_session_id: str | None = None,
    ) -> ScrapedPage:
        # initialize browser state
        # TODO: add workflow_run_id or eventually script_id/script_run_id
        browser_state = await cls._get_or_create_browser_state(browser_session_id=browser_session_id)
        return await browser_state.scrape_website(
            url="",
            cleanup_element_tree=app.AGENT_FUNCTION.cleanup_element_tree_factory(),
            scrape_exclude=app.scrape_exclude,
            max_screenshot_number=settings.MAX_NUM_SCREENSHOTS,
            draw_boxes=True,
            scroll=True,
            support_empty_page=True,
        )

    async def _ensure_download_to_complete(
        self,
        download_dir: Path,
        browser_session_id: str | None = None,
    ) -> None:
        context = skyvern_context.current()
        if not context or not context.organization_id:
            return
        if not download_dir.exists():
            return
        organization_id = context.organization_id
        download_timeout = BROWSER_DOWNLOAD_TIMEOUT
        if context.task_id:
            task = await app.DATABASE.get_task(context.task_id, organization_id=organization_id)
            if task and task.download_timeout:
                download_timeout = task.download_timeout
        await check_downloading_files_and_wait_for_download_to_complete(
            download_dir=download_dir,
            organization_id=organization_id,
            browser_session_id=browser_session_id,
            timeout=download_timeout,
        )

    async def _decorate_call(
        self,
        fn: Callable,
        action: ActionType,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """
        Decorator to record the action call.

        Auto-creates action records in DB before action execution
        and screenshot artifacts after action execution.
        """

        # Emoji mapping for different action types
        ACTION_EMOJIS = {
            ActionType.CLICK: "👆",
            ActionType.INPUT_TEXT: "⌨️",
            ActionType.UPLOAD_FILE: "📤",
            ActionType.DOWNLOAD_FILE: "📥",
            ActionType.HOVER: "🖱️",
            ActionType.SELECT_OPTION: "🎯",
            ActionType.WAIT: "⏳",
            ActionType.SOLVE_CAPTCHA: "🔓",
            ActionType.VERIFICATION_CODE: "🔐",
            ActionType.SCROLL: "📜",
            ActionType.COMPLETE: "✅",
            ActionType.TERMINATE: "🛑",
        }

        prompt = kwargs.get("prompt", "")

        # Backward compatibility: use intention if provided and prompt is empty
        intention = kwargs.get("intention", None)
        if intention and not prompt:
            prompt = intention

        data = kwargs.get("data", None)
        meta = ActionMetadata(prompt, data)
        call = ActionCall(action, args, kwargs, meta)

        action_status = ActionStatus.completed

        # Print action in script mode
        context = skyvern_context.current()
        if context and context.script_mode:
            emoji = ACTION_EMOJIS.get(action, "🔧")
            action_name = action.value if hasattr(action, "value") else str(action)
            print(f"{emoji} {action_name.replace('_', ' ').title()}", end="")
            if prompt:
                print(f": {prompt}")
            else:
                print()

        # Download detection for click actions
        download_triggered: bool | None = None
        downloaded_files: list[str] | None = None
        files_before: list[str] = []
        download_dir: Path | None = None

        # Capture files before click action for download detection
        if action == ActionType.CLICK and context and context.workflow_run_id:
            try:
                download_dir = get_path_for_workflow_download_directory(context.workflow_run_id)
                if download_dir.exists():
                    files_before = list_files_in_directory(download_dir)
                if context.browser_session_id and context.organization_id:
                    browser_session_downloaded_files = await app.STORAGE.list_downloaded_files_in_browser_session(
                        organization_id=context.organization_id,
                        browser_session_id=context.browser_session_id,
                    )
                    files_before = files_before + browser_session_downloaded_files

            except Exception:
                pass  # Don't block action execution if file listing fails

        try:
            call.result = await fn(self, *args, **kwargs)

            # Note: Action status would be updated to completed here if update method existed

            # Print success in script mode
            if context and context.script_mode:
                print("  ✓ Completed")

            return call.result
        except Exception as e:
            call.error = e
            action_status = ActionStatus.failed
            # Note: Action status would be updated to failed here if update method existed

            # Print failure in script mode
            if context and context.script_mode:
                print(f"  ✗ Failed: {str(e)}")

            # LLM fallback hook could go here ...
            raise
        finally:
            # Add a small buffer between cached actions to give slow pages time to settle
            if settings.CACHED_ACTION_DELAY_SECONDS > 0:
                await asyncio.sleep(settings.CACHED_ACTION_DELAY_SECONDS)

            # Check for downloaded files after click action
            if action == ActionType.CLICK and context and context.workflow_run_id and download_dir:
                try:
                    if download_dir.exists():
                        await self._ensure_download_to_complete(
                            download_dir=download_dir,
                            browser_session_id=context.browser_session_id,
                        )
                        files_after = list_files_in_directory(download_dir)
                        if context.browser_session_id and context.organization_id:
                            browser_session_downloaded_files = (
                                await app.STORAGE.list_downloaded_files_in_browser_session(
                                    organization_id=context.organization_id,
                                    browser_session_id=context.browser_session_id,
                                )
                            )
                            files_after = files_after + browser_session_downloaded_files

                        new_file_paths = set(files_after) - set(files_before)
                        if new_file_paths:
                            download_triggered = True
                            downloaded_files = [os.path.basename(fp) for fp in new_file_paths]
                            LOG.info(
                                "Script click action detected download",
                                downloaded_files=downloaded_files,
                                workflow_run_id=context.workflow_run_id,
                            )
                        else:
                            download_triggered = False
                except Exception:
                    pass  # Don't block if download detection fails

            self._record(call)
            # Auto-create action after execution and store result
            await self._create_action_and_result_after_execution(
                action_type=action,
                intention=prompt,
                status=action_status,
                kwargs=kwargs,
                call_result=call.result,
                call_error=call.error,
                download_triggered=download_triggered,
                downloaded_files=downloaded_files,
            )

            # Auto-create screenshot artifact after execution
            await self._create_screenshot_after_execution()

    async def _update_action_reasoning(
        self,
        action_id: str,
        organization_id: str,
        action_type: ActionType,
        intention: str = "",
        text: str | None = None,
        select_option: SelectOption | None = None,
        file_url: str | None = None,
        data_extraction_goal: str | None = None,
        data_extraction_schema: dict[str, Any] | list | str | None = None,
    ) -> str:
        """Generate user-facing reasoning for an action using the secondary LLM."""

        reasoning = f"Auto-generated action for {action_type.value}"
        try:
            context = skyvern_context.current()
            if not context or not context.organization_id:
                return f"Auto-generated action for {action_type.value}"

            # Build the prompt with available context
            reasoning_prompt = prompt_engine.load_prompt(
                template="generate-action-reasoning",
                action_type=action_type.value,
                intention=intention,
                text=text,
                select_option=select_option.value if select_option else None,
                file_url=file_url,
                data_extraction_goal=data_extraction_goal,
                data_extraction_schema=data_extraction_schema,
            )

            # Call secondary LLM to generate reasoning
            json_response = await app.SECONDARY_LLM_API_HANDLER(
                prompt=reasoning_prompt,
                prompt_name="generate-action-reasoning",
                organization_id=context.organization_id,
            )

            reasoning = json_response.get("reasoning", f"Auto-generated action for {action_type.value}")

        except Exception:
            LOG.warning("Failed to generate action reasoning, using fallback", action_type=action_type)
        await app.DATABASE.update_action_reasoning(
            organization_id=organization_id,
            action_id=action_id,
            reasoning=reasoning,
        )
        return reasoning

    async def _create_action_and_result_after_execution(
        self,
        action_type: ActionType,
        intention: str = "",
        status: ActionStatus = ActionStatus.pending,
        kwargs: dict[str, Any] | None = None,
        call_result: Any | None = None,
        call_error: Exception | None = None,
        download_triggered: bool | None = None,
        downloaded_files: list[str] | None = None,
    ) -> tuple[Action | None, list[ActionResult]]:
        """Create an action record and result in the database after execution if task_id and step_id are available.

        Returns a tuple of (Action, list[ActionResult]) similar to how the agent stores actions and results.
        """
        results: list[ActionResult] = []

        try:
            context = skyvern_context.current()
            if not context or not context.task_id or not context.step_id:
                return None, results

            # Create action record. TODO: store more action fields
            kwargs = kwargs or {}
            # we're using "value" instead of "text" for input text actions interface
            xpath = None
            if action_type == ActionType.CLICK:
                if isinstance(call_result, str) and "xpath=" in call_result:
                    xpath_split_list = call_result.split("xpath=")
                    if len(xpath_split_list) > 1:
                        xpath = xpath_split_list[1]
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
                text=text,
                option=select_option,
                file_url=file_url,
                response=response,
                xpath=xpath,
                download=download_triggered,
                download_triggered=download_triggered,
                downloaded_files=downloaded_files,
                created_by="script",
            )
            data_extraction_goal = None
            data_extraction_schema = None
            if action_type == ActionType.EXTRACT:
                data_extraction_goal = kwargs.get("prompt")
                data_extraction_schema = kwargs.get("schema")
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
                    data_extraction_goal=data_extraction_goal,
                    data_extraction_schema=data_extraction_schema,
                    option=select_option,
                    response=response,
                    created_by="script",
                )

            created_action = await app.DATABASE.create_action(action)
            # Generate user-facing reasoning using secondary LLM
            asyncio.create_task(
                self._update_action_reasoning(
                    action_id=str(created_action.action_id),
                    organization_id=str(context.organization_id),
                    action_type=action_type,
                    intention=intention,
                    text=text,
                    select_option=select_option,
                    file_url=file_url,
                    data_extraction_goal=data_extraction_goal,
                    data_extraction_schema=data_extraction_schema,
                )
            )

            context.action_order += 1

            # Create ActionResult based on success/failure
            if call_error:
                result = ActionFailure(exception=call_error, download_triggered=download_triggered)
            else:
                # For extract actions, include the extracted data in the result
                result_data = None
                if action_type == ActionType.EXTRACT and call_result:
                    result_data = call_result
                result = ActionSuccess(
                    data=result_data,
                    download_triggered=download_triggered,
                    downloaded_files=downloaded_files,
                )

            results = [result]

            # Store action and results in RunContext for step output
            run_context = script_run_context_manager.get_run_context()
            if run_context:
                run_context.actions_and_results.append((created_action, results))

            return created_action, results

        except Exception:
            # If action creation fails, don't block the actual action execution
            return None, results

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

    async def get_actual_value(
        self,
        value: str,
        totp_identifier: str | None = None,
        totp_url: str | None = None,
    ) -> str:
        """Input text into an element identified by ``selector``."""
        context = skyvern_context.ensure_context()
        if context and context.workflow_run_id:
            task_id = context.task_id
            workflow_run_id = context.workflow_run_id
            organization_id = context.organization_id
            original_value = value
            value = get_actual_value_of_parameter_if_secret(workflow_run_id, original_value)

            # support TOTP secret and internal it to TOTP code
            is_totp_value = value == "BW_TOTP" or value == "OP_TOTP" or value == "AZ_TOTP"
            if is_totp_value:
                value = generate_totp_value(context.workflow_run_id, original_value)
            elif (totp_identifier or totp_url) and organization_id:
                totp_value = await poll_otp_value(
                    organization_id=organization_id,
                    task_id=task_id,
                    workflow_run_id=workflow_run_id,
                    totp_verification_url=totp_url,
                    totp_identifier=totp_identifier,
                )
                if totp_value:
                    # use the totp verification code
                    value = totp_value.value

        return value

    async def goto(self, url: str, **kwargs: Any) -> None:
        url = render_template(url)
        url = prepend_scheme_and_validate_url(url)

        # Print navigation in script mode
        context = skyvern_context.current()
        if context and context.script_mode:
            print(f"🌐 Navigating to: {url}")

        timeout = kwargs.pop("timeout", settings.BROWSER_LOADING_TIMEOUT_MS)
        await self.page.goto(url, timeout=timeout, **kwargs)

        if context and context.script_mode:
            print("  ✓ Page loaded")

    @action_wrap(ActionType.SOLVE_CAPTCHA)
    async def solve_captcha(
        self, prompt: str | None = None, data: str | dict[str, Any] | None = None, intention: str | None = None
    ) -> None:
        context = skyvern_context.current()
        if not context or not context.organization_id or not context.task_id or not context.step_id:
            await asyncio.sleep(30)
            return None

        task = await app.DATABASE.get_task(context.task_id, context.organization_id)
        step = await app.DATABASE.get_step(context.step_id, context.organization_id)
        if task and step:
            solve_captcha_handler = ActionHandler._handled_action_types[ActionType.SOLVE_CAPTCHA]
            action = SolveCaptchaAction(
                organization_id=context.organization_id,
                task_id=context.task_id,
                step_id=context.step_id,
            )
            await solve_captcha_handler(action, self.page, self.scraped_page, task, step)
        else:
            await asyncio.sleep(30)

    @action_wrap(ActionType.COMPLETE)
    async def complete(
        self, prompt: str | None = None, data: str | dict[str, Any] | None = None, intention: str | None = None
    ) -> None:
        # TODO: add validation here. if it doesn't pass the validation criteria:
        #  1. terminate the workflow run if fallback to ai is false
        #  2. fallback to ai if fallback to ai is true
        context = skyvern_context.current()
        if (
            not context
            or not context.organization_id
            or not context.workflow_run_id
            or not context.task_id
            or not context.step_id
        ):
            return
        task = await app.DATABASE.get_task(context.task_id, context.organization_id)
        step = await app.DATABASE.get_step(context.step_id, context.organization_id)
        if task and step:
            # CRITICAL: Update step.output with actions_and_results BEFORE validation
            # This ensures complete_verify() can access action history (including download info)
            # when checking if the goal was achieved
            await self._update_step_output_before_complete(context)
            # Refresh step to get updated output for validation
            step = await app.DATABASE.get_step(context.step_id, context.organization_id)
            if not step:
                return

            action = CompleteAction(
                organization_id=context.organization_id,
                task_id=context.task_id,
                step_id=context.step_id,
                step_order=step.order,
                action_order=context.action_order,
            )
            # result = await ActionHandler.handle_action(self.scraped_page, task, step, self.page, action)
            result = await handle_complete_action(action, self.page, self.scraped_page, task, step)
            if result and result[-1].success is False:
                raise ScriptTerminationException(result[-1].exception_message)

    async def _update_step_output_before_complete(self, context: skyvern_context.SkyvernContext) -> None:
        """Update step.output with actions_and_results before complete validation.

        This is critical for cached runs because complete_verify() reads action history
        from step.output.actions_and_results to check if goals were achieved (e.g., file downloads).
        Without this, the validation has no visibility into what actions were performed.
        """

        # Validate required context fields
        if not context.step_id or not context.task_id or not context.organization_id:
            return

        run_context = script_run_context_manager.get_run_context()
        if not run_context or not run_context.actions_and_results:
            return

        # Extract errors from DecisiveActions (similar to agent flow)
        errors: list[UserDefinedError] = []
        for action, _ in run_context.actions_and_results:
            if isinstance(action, DecisiveAction):
                errors.extend(action.errors)

        # Create AgentStepOutput similar to how agent does it
        step_output = AgentStepOutput(
            actions_and_results=run_context.actions_and_results,
            action_results=[result for _, results in run_context.actions_and_results for result in results],
            errors=errors,
        )

        await app.DATABASE.update_step(
            step_id=context.step_id,
            task_id=context.task_id,
            organization_id=context.organization_id,
            output=step_output,
        )
        LOG.info(
            "Updated step output with cached actions before complete validation",
            step_id=context.step_id,
            task_id=context.task_id,
            num_actions=len(run_context.actions_and_results),
        )


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
