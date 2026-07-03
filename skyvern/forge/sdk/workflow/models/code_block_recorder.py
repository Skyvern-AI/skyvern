from __future__ import annotations

import sys
import time
from types import FrameType
from typing import Any, Awaitable, Callable

import structlog

from skyvern.webeye.actions.action_types import ActionType
from skyvern.webeye.actions.actions import Action, ActionStatus

LOG = structlog.get_logger()

CODE_BLOCK_FILENAME = "<code_block>"
# full_code = "\nasync def wrapper(...):\n<user code from line 3>"; user line = frame line - 2
CODE_LINE_OFFSET = 2

_PAGE_ACTION_MAP: dict[str, ActionType] = {
    "goto": ActionType.GOTO_URL,
    "go_back": ActionType.GO_BACK,
    "go_forward": ActionType.GO_FORWARD,
    "reload": ActionType.RELOAD_PAGE,
    "evaluate": ActionType.EXECUTE_JS,
}
_LOCATOR_ACTION_MAP: dict[str, ActionType] = {
    "click": ActionType.CLICK,
    "dblclick": ActionType.CLICK,
    "fill": ActionType.INPUT_TEXT,
    "type": ActionType.INPUT_TEXT,
    "press": ActionType.KEYPRESS,
    "select_option": ActionType.SELECT_OPTION,
    "check": ActionType.CHECKBOX,
    "uncheck": ActionType.CHECKBOX,
    "hover": ActionType.HOVER,
    "set_input_files": ActionType.UPLOAD_FILE,
}
# Sync locator factories on both Page and Locator; their results must stay wrapped.
_LOCATOR_FACTORY_METHODS = frozenset(
    {
        "get_by_role",
        "get_by_text",
        "get_by_label",
        "get_by_placeholder",
        "get_by_alt_text",
        "get_by_title",
        "get_by_test_id",
        "filter",
    }
)
# SkyvernPage high-level API (page.extract / page.complete / ...). These are not raw
# Playwright calls, so they fall through the maps above and used to execute unrecorded —
# a navigate+extract block then rendered as only repeated "Goto URL" on the timeline.
# Mirror skyvern_page.py's @action_wrap table and the editor deriver
# (code_block_steps._METHOD_ACTION_TYPES) so extraction and the rest of the surface
# record as distinct, reader-facing steps.
_HIGH_LEVEL_ACTION_MAP: dict[str, ActionType] = {
    "extract": ActionType.EXTRACT,
    "complete": ActionType.COMPLETE,
    "terminate": ActionType.TERMINATE,
    "wait": ActionType.WAIT,
    "reload_page": ActionType.RELOAD_PAGE,
    "scroll": ActionType.SCROLL,
    "keypress": ActionType.KEYPRESS,
    "move": ActionType.MOVE,
    "drag": ActionType.DRAG,
    "left_mouse": ActionType.LEFT_MOUSE,
    "download_file": ActionType.DOWNLOAD_FILE,
    "solve_captcha": ActionType.SOLVE_CAPTCHA,
    "verification_code": ActionType.VERIFICATION_CODE,
    "upload_file": ActionType.UPLOAD_FILE,
    "fill_autocomplete": ActionType.INPUT_TEXT,
}
# High-level methods whose natural-language `prompt` (positional or keyword) is the
# reader-facing description; mirrors code_block_steps._PROMPT_POSITIONAL_METHODS.
_PROMPT_METHODS: frozenset[str] = frozenset({"extract", "complete", "solve_captcha", "verification_code"})

OnAction = Callable[[Action], Awaitable[None]]


def _frame_user_line() -> int | None:
    # Walk f_back instead of inspect.stack(), which reads source for every frame; this runs on
    # every recorded action, on the await chain that counts against the code block timeout.
    frame: FrameType | None = sys._getframe()
    while frame is not None:
        if frame.f_code.co_filename == CODE_BLOCK_FILENAME:
            return max(frame.f_lineno - CODE_LINE_OFFSET, 1)
        frame = frame.f_back
    return None


def user_code_line_from_exception(exc: BaseException) -> int | None:
    tb = exc.__traceback__
    line: int | None = None
    while tb is not None:
        if tb.tb_frame.f_code.co_filename == CODE_BLOCK_FILENAME:
            line = max(tb.tb_lineno - CODE_LINE_OFFSET, 1)
        tb = tb.tb_next
    return line


def _describe(name: str, target: str | None, args: tuple[Any, ...]) -> str:
    arg = next((str(a) for a in args if isinstance(a, (str, int, float))), None)
    parts = [name]
    if target:
        parts.append(target)
    if arg is not None and arg != target:
        parts.append(arg[:200])
    return " ".join(parts)


def _factory_selector(name: str, args: tuple[Any, ...]) -> str:
    arg = next((str(a) for a in args if isinstance(a, (str, int, float))), None)
    return f"{name}({arg})" if arg is not None else name


class _Recorder:
    def __init__(self, on_action: OnAction | None = None) -> None:
        self.actions: list[Action] = []
        self.last_exception: BaseException | None = None
        self._on_action = on_action

    async def record(
        self,
        action_type: ActionType,
        name: str,
        target: str | None,
        call: Callable[[], Awaitable[Any]],
        args: tuple[Any, ...],
        description: str | None = None,
    ) -> Any:
        started = time.monotonic()
        # Input values may be credentials (incl. derived TOTP codes); never describe them.
        describe_args = () if action_type == ActionType.INPUT_TEXT else args
        action = Action(
            action_type=action_type,
            status=ActionStatus.completed,
            action_order=len(self.actions),
            # A reader-facing prompt (page.extract/complete) is the action's own copy; prefer it over
            # the "page.method arg" form so the timeline reads as plain language even when the editor's
            # derived step is missing or stale and the UI falls back to this description.
            description=description if description is not None else _describe(name, target, describe_args),
            output={"code_line": _frame_user_line()},
        )
        try:
            result = await call()
        except BaseException as exc:
            action.status = ActionStatus.failed
            action.response = str(exc)[:500]
            self.last_exception = exc
            raise
        finally:
            duration_ms = int((time.monotonic() - started) * 1000)
            if isinstance(action.output, dict):
                action.output["duration_ms"] = duration_ms
            self.actions.append(action)
            if self._on_action is not None:
                try:
                    await self._on_action(action)
                except Exception:
                    LOG.warning("Code block action sink failed", action_type=action_type, exc_info=True)
        return result


class RecordingLocator:
    def __init__(self, locator: Any, recorder: _Recorder, selector: str | None) -> None:
        self.__locator = locator
        self.__recorder = recorder
        self.__selector = selector

    def locator(self, selector: str, **kwargs: Any) -> RecordingLocator:
        return RecordingLocator(self.__locator.locator(selector, **kwargs), self.__recorder, selector)

    @property
    def first(self) -> RecordingLocator:
        return RecordingLocator(self.__locator.first, self.__recorder, self.__selector)

    def nth(self, index: int) -> RecordingLocator:
        return RecordingLocator(self.__locator.nth(index), self.__recorder, self.__selector)

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self.__locator, name)
        if name in _LOCATOR_FACTORY_METHODS and callable(attr):

            def factory(*args: Any, **kwargs: Any) -> RecordingLocator:
                return RecordingLocator(attr(*args, **kwargs), self.__recorder, _factory_selector(name, args))

            return factory
        action_type = _LOCATOR_ACTION_MAP.get(name)
        if action_type is None or not callable(attr):
            return attr

        async def recorded(*args: Any, **kwargs: Any) -> Any:
            return await self.__recorder.record(
                action_type, f"locator.{name}", self.__selector, lambda: attr(*args, **kwargs), args
            )

        return recorded


class RecordingKeyboard:
    def __init__(self, keyboard: Any, recorder: _Recorder) -> None:
        self.__keyboard = keyboard
        self.__recorder = recorder

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self.__keyboard, name)
        if name != "press" or not callable(attr):
            return attr

        async def recorded(*args: Any, **kwargs: Any) -> Any:
            return await self.__recorder.record(
                ActionType.KEYPRESS, "keyboard.press", None, lambda: attr(*args, **kwargs), args
            )

        return recorded


class RecordingPage:
    """Proxy around a playwright page that records mapped calls as Actions.

    Name-mangled private state keeps casual user code away from recorder
    internals, but the mangled `_RecordingPage__*` form is still reachable;
    treat recordings as telemetry, not a tamper-proof audit trail.
    """

    def __init__(self, page: Any, on_action: OnAction | None = None) -> None:
        self.__page = page
        self.__recorder = _Recorder(on_action)

    def recorded_actions(self) -> list[Action]:
        return list(self.__recorder.actions)

    def last_recorded_exception(self) -> BaseException | None:
        return self.__recorder.last_exception

    def locator(self, selector: str, **kwargs: Any) -> RecordingLocator:
        return RecordingLocator(self.__page.locator(selector, **kwargs), self.__recorder, selector)

    @property
    def keyboard(self) -> RecordingKeyboard:
        return RecordingKeyboard(self.__page.keyboard, self.__recorder)

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self.__page, name)
        if name in _LOCATOR_FACTORY_METHODS and callable(attr):

            def factory(*args: Any, **kwargs: Any) -> RecordingLocator:
                return RecordingLocator(attr(*args, **kwargs), self.__recorder, _factory_selector(name, args))

            return factory
        # Record direct page-level interactions (page.click/fill/press/...) and the high-level
        # SkyvernPage API (page.extract/complete/...) with the same redaction as the locator path.
        action_type = _LOCATOR_ACTION_MAP.get(name) or _PAGE_ACTION_MAP.get(name) or _HIGH_LEVEL_ACTION_MAP.get(name)
        if action_type is None or not callable(attr):
            return attr
        record_prompt = name in _PROMPT_METHODS

        async def recorded(*args: Any, **kwargs: Any) -> Any:
            description: str | None = None
            if record_prompt:
                prompt = kwargs.get("prompt", args[0] if args else None)
                if isinstance(prompt, str) and prompt.strip():
                    description = " ".join(prompt.split())[:200]
            return await self.__recorder.record(
                action_type, f"page.{name}", None, lambda: attr(*args, **kwargs), args, description=description
            )

        return recorded
