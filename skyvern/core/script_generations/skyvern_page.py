from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Callable, Literal

from playwright.async_api import Page

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.api.files import download_file
from skyvern.forge.sdk.core import skyvern_context
from skyvern.webeye.actions import handler_utils
from skyvern.webeye.actions.action_types import ActionType
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
    async def create(cls) -> SkyvernPage:
        # initialize browser state
        browser_state = await app.BROWSER_MANAGER.get_or_create_for_script()
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

        TODOs:
        - generate action record in db pre action
        - generate screenshot post action
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
                try:
                    call.result = await fn(
                        skyvern_page, *args, intention=intention, data=data, **kwargs
                    )  # real driver call
                    return call.result
                except Exception as e:
                    call.error = e
                    # LLM fallback hook could go here ...
                    raise
                finally:
                    skyvern_page._record(call)

            return wrapper

        return decorator

    async def goto(self, url: str) -> None:
        await self.page.goto(url)

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
        new_text = text

        if intention and data:
            try:
                # Build the element tree of the current page for the prompt
                skyvern_context.ensure_context()
                payload_str = json.dumps(data) if isinstance(data, (dict, list)) else (data or "")
                script_generation_input_text_prompt = prompt_engine.load_prompt(
                    template="script-generation-input-text-generatiion",
                    intention=intention,
                    data=payload_str,
                )
                json_response = await app.SINGLE_INPUT_AGENT_LLM_API_HANDLER(
                    prompt=script_generation_input_text_prompt,
                    prompt_name="script-generation-input-text-generatiion",
                )
                new_text = json_response.get("answer", text) or text
            except Exception:
                # If anything goes wrong, fall back to the original text
                new_text = text

        locator = self.page.locator(f"xpath={xpath}")
        await handler_utils.input_sequentially(locator, new_text, timeout=timeout)

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
        self, data_extraction_goal: str, intention: str | None = None, data: str | dict[str, Any] | None = None
    ) -> None:
        # TODO: extract the data
        return

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
    def __init__(self, parameters: dict[str, Any], page: SkyvernPage) -> None:
        self.parameters = parameters
        self.page = page
        self.trace: list[ActionCall] = []
        self.prompt: str | None = None
