from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Callable

from playwright.async_api import Page

from skyvern.config import settings
from skyvern.forge.sdk.api.files import download_file
from skyvern.webeye.actions import handler_utils
from skyvern.webeye.actions.action_types import ActionType


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
        page: Page,
        driver: Driver = Driver.PLAYWRIGHT,
        *,
        recorder: Callable[[ActionCall], None] | None = None,
    ):
        self.driver = driver
        self.page = page  # e.g. Playwright's Page
        self._record = recorder or (lambda ac: None)

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
                    call.result = await fn(skyvern_page, *args, **kwargs)  # real driver call
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
        locator = self.page.locator(xpath)
        await locator.click(timeout=5000)

    @action_wrap(ActionType.INPUT_TEXT)
    async def input_text(
        self,
        xpath: str,
        text: str,
        intention: str | None = None,
        data: str | dict[str, Any] | None = None,
        timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS,
    ) -> None:
        locator = self.page.locator(xpath)
        await handler_utils.input_sequentially(locator, text, timeout=timeout)

    @action_wrap(ActionType.UPLOAD_FILE)
    async def upload_file(
        self, xpath: str, file_path: str, intention: str | None = None, data: str | dict[str, Any] | None = None
    ) -> None:
        file = await download_file(file_path)
        await self.page.set_input_files(xpath, file)

    @action_wrap(ActionType.SELECT_OPTION)
    async def select_option(
        self, xpath: str, option: str, intention: str | None = None, data: str | dict[str, Any] | None = None
    ) -> None:
        locator = self.page.locator(xpath)
        await locator.select_option(option, timeout=5000)

    @action_wrap(ActionType.WAIT)
    async def wait(
        self, seconds: float, intention: str | None = None, data: str | dict[str, Any] | None = None
    ) -> None: ...

    @action_wrap(ActionType.NULL_ACTION)
    async def null_action(self, intention: str | None = None, data: str | dict[str, Any] | None = None) -> None: ...

    @action_wrap(ActionType.SOLVE_CAPTCHA)
    async def solve_captcha(
        self, xpath: str, intention: str | None = None, data: str | dict[str, Any] | None = None
    ) -> None: ...

    @action_wrap(ActionType.TERMINATE)
    async def terminate(
        self, errors: list[str], intention: str | None = None, data: str | dict[str, Any] | None = None
    ) -> None: ...

    @action_wrap(ActionType.COMPLETE)
    async def complete(
        self, data_extraction_goal: str, intention: str | None = None, data: str | dict[str, Any] | None = None
    ) -> None: ...

    @action_wrap(ActionType.RELOAD_PAGE)
    async def reload_page(self, intention: str | None = None, data: str | dict[str, Any] | None = None) -> None: ...

    @action_wrap(ActionType.EXTRACT)
    async def extract(
        self, data_extraction_goal: str, intention: str | None = None, data: str | dict[str, Any] | None = None
    ) -> None: ...

    @action_wrap(ActionType.VERIFICATION_CODE)
    async def verification_code(
        self, xpath: str, intention: str | None = None, data: str | dict[str, Any] | None = None
    ) -> None: ...

    @action_wrap(ActionType.SCROLL)
    async def scroll(
        self, amount: int, intention: str | None = None, data: str | dict[str, Any] | None = None
    ) -> None: ...

    @action_wrap(ActionType.KEYPRESS)
    async def keypress(
        self, key: str, intention: str | None = None, data: str | dict[str, Any] | None = None
    ) -> None: ...

    @action_wrap(ActionType.TYPE)
    async def type(self, text: str, intention: str | None = None, data: str | dict[str, Any] | None = None) -> None: ...

    @action_wrap(ActionType.MOVE)
    async def move(
        self, x: int, y: int, intention: str | None = None, data: str | dict[str, Any] | None = None
    ) -> None: ...

    @action_wrap(ActionType.DRAG)
    async def drag(
        self,
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        intention: str | None = None,
        data: str | dict[str, Any] | None = None,
    ) -> None: ...

    @action_wrap(ActionType.LEFT_MOUSE)
    async def left_mouse(
        self, x: int, y: int, intention: str | None = None, data: str | dict[str, Any] | None = None
    ) -> None: ...


class RunContext:
    """
    Lives for one workflow run.
    """

    def __init__(self, parameters: dict[str, Any], page: SkyvernPage) -> None:
        self.parameters = parameters
        self.page = page
        self.trace: list[ActionCall] = []
