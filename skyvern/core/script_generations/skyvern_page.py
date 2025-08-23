from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Callable, Literal

from playwright.async_api import Page

from skyvern.config import settings
from skyvern.exceptions import WorkflowRunNotFound
from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.api.files import download_file
from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.core import skyvern_context
from skyvern.utils.prompt_engine import load_prompt_with_elements
from skyvern.webeye.actions import handler_utils
from skyvern.webeye.actions.action_types import ActionType
from skyvern.webeye.actions.actions import Action, ActionStatus
from skyvern.webeye.browser_factory import BrowserState
from skyvern.webeye.scraper.scraper import ScrapedPage, scrape_website


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

    @classmethod
    async def _get_or_create_browser_state(cls) -> BrowserState:
        context = skyvern_context.current()
        if context and context.workflow_run_id and context.organization_id:
            workflow_run = await app.DATABASE.get_workflow_run(
                workflow_run_id=context.workflow_run_id, organization_id=context.organization_id
            )
            if workflow_run:
                browser_state = await app.BROWSER_MANAGER.get_or_create_for_workflow_run(
                    workflow_run=workflow_run, browser_session_id=None
                )
            else:
                raise WorkflowRunNotFound(workflow_run_id=context.workflow_run_id)
        else:
            browser_state = await app.BROWSER_MANAGER.get_or_create_for_script()
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
    async def create(cls) -> SkyvernPage:
        # initialize browser state
        # TODO: add workflow_run_id or eventually script_id/script_run_id
        browser_state = await cls._get_or_create_browser_state()
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
                    await skyvern_page._create_action_before_execution(
                        action_type=action,
                        intention=intention,
                        status=action_status,
                        data=data,
                    )

                    # Auto-create screenshot artifact after execution
                    await skyvern_page._create_screenshot_after_execution()

            return wrapper

        return decorator

    async def goto(self, url: str) -> None:
        await self.page.goto(url)

    async def _create_action_before_execution(
        self,
        action_type: ActionType,
        intention: str = "",
        status: ActionStatus = ActionStatus.pending,
        data: str | dict[str, Any] = "",
    ) -> Action | None:
        """Create an action record in the database before execution if task_id and step_id are available."""
        try:
            context = skyvern_context.current()
            if not context or not context.task_id or not context.step_id:
                return None

            # Create action record. TODO: store more action fields
            action = Action(
                action_type=action_type,
                status=status,
                organization_id=context.organization_id,
                workflow_run_id=context.workflow_run_id,
                task_id=context.task_id,
                step_id=context.step_id,
                step_order=0,  # Will be updated by the system if needed
                action_order=0,  # Will be updated by the system if needed
                intention=intention,
                reasoning=f"Auto-generated action for {action_type.value}",
            )

            created_action = await app.DATABASE.create_action(action)
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
                    context.task_id, context.step_id, organization_id=context.organization_id
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

        if intention and data:
            try:
                # Build the element tree of the current page for the prompt
                context = skyvern_context.ensure_context()
                payload_str = json.dumps(data) if isinstance(data, (dict, list)) else (data or "")
                refreshed_page = await self.scraped_page.generate_scraped_page_without_screenshots()
                element_tree = refreshed_page.build_element_tree()
                single_click_prompt = prompt_engine.load_prompt(
                    template="single-click-action",
                    navigation_goal=intention,
                    navigation_payload_str=payload_str,
                    current_url=self.page.url,
                    elements=element_tree,
                    local_datetime=datetime.now(context.tz_info or datetime.now().astimezone().tzinfo).isoformat(),
                    user_context=getattr(context, "prompt", None),
                )
                json_response = await app.SINGLE_CLICK_AGENT_LLM_API_HANDLER(
                    prompt=single_click_prompt,
                    prompt_name="single-click-action",
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
        text: str,
        intention: str | None = None,
        data: str | dict[str, Any] | None = None,
        timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS,
    ) -> None:
        await self._input_text(xpath, text, intention, data, timeout)

    @action_wrap(ActionType.INPUT_TEXT)
    async def type(
        self,
        xpath: str,
        text: str,
        intention: str | None = None,
        data: str | dict[str, Any] | None = None,
        timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS,
    ) -> None:
        await self._input_text(xpath, text, intention, data, timeout)

    async def _input_text(
        self,
        xpath: str,
        text: str,
        intention: str | None = None,
        data: str | dict[str, Any] | None = None,
        timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS,
    ) -> None:
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
        if context and context.workflow_run_id:
            text = await _get_actual_value_of_parameter_if_secret(context.workflow_run_id, text)

        locator = self.page.locator(f"xpath={xpath}")
        await handler_utils.input_sequentially(locator, text, timeout=timeout)

    @action_wrap(ActionType.UPLOAD_FILE)
    async def upload_file(
        self, xpath: str, file_path: str, intention: str | None = None, data: str | dict[str, Any] | None = None
    ) -> None:
        # if self.generate_response:
        #     # TODO: regenerate file_path and xpath
        #     pass
        file = await download_file(file_path)
        await self.page.set_input_files(xpath, file)

    @action_wrap(ActionType.SELECT_OPTION)
    async def select_option(
        self,
        xpath: str,
        option: str,
        intention: str | None = None,
        data: str | dict[str, Any] | None = None,
        timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS,
    ) -> None:
        # if self.generate_response:
        #     # TODO: regenerate option
        #     pass
        locator = self.page.locator(f"xpath={xpath}")
        try:
            await locator.click(timeout=timeout)
        except Exception:
            print("Failed to click before select action")
            return
        await locator.select_option(option, timeout=timeout)

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
                task_id=context.task_id, step_id=context.step_id, organization_id=context.organization_id
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
    ) -> None: ...

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
        # if generated_parameters:
        #     self.parameters.update(generated_parameters)
        self.page = page
        self.trace: list[ActionCall] = []
        self.prompt: str | None = None


async def _get_actual_value_of_parameter_if_secret(workflow_run_id: str, parameter: str) -> Any:
    """
    Get the actual value of a parameter if it's a secret. If it's not a secret, return the parameter value as is.

    Just return the parameter value if the task isn't a workflow's task.

    This is only used for InputTextAction, UploadFileAction, and ClickAction (if it has a file_url).
    """
    workflow_run_context = app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context(workflow_run_id)
    secret_value = workflow_run_context.get_original_secret_value_or_none(parameter)
    return secret_value if secret_value is not None else parameter
