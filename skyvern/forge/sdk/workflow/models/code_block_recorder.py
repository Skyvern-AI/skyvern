from __future__ import annotations

import inspect
import sys
import time
from os import PathLike, fspath
from types import FrameType
from typing import Any, Awaitable, Callable

import pydantic
import structlog

from skyvern.webeye.actions.action_types import ActionType
from skyvern.webeye.actions.actions import Action, ActionStatus, SelectOption

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
_RECORDABLE_HANDLE_TYPE_NAMES = frozenset({"ElementHandle", "FrameLocator", "Locator"})
# SkyvernPage high-level API (page.extract / page.complete / ...). These are not raw
# Playwright calls, so they fall through the maps above and used to execute unrecorded —
# a navigate+extract block then rendered as only repeated "Goto URL" on the timeline.
# Mirror skyvern_page.py's @action_wrap table and the editor deriver
# (code_block_steps._METHOD_ACTION_TYPES) so extraction and the rest of the surface
# record as distinct, reader-facing steps.
# `extract` is absent on purpose: code blocks run on a raw Playwright page and must not reach
# the LLM extraction path, so nothing may author or record a page.extract call.
_HIGH_LEVEL_ACTION_MAP: dict[str, ActionType] = {
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
_PROMPT_METHODS: frozenset[str] = frozenset({"complete", "solve_captcha", "verification_code"})

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


def _string_value(value: Any) -> str | None:
    if isinstance(value, (str, int, float)):
        return str(value)
    if isinstance(value, PathLike):
        return fspath(value)
    return None


def _arg(args: tuple[Any, ...], index: int) -> Any:
    return args[index] if len(args) > index else None


def _page_value_index(name: str, target: str | None) -> int:
    # Direct Playwright page web actions take selector first, value second.
    return 1 if target is None and name.startswith("page.") else 0


def _element_id(name: str, target: str | None, args: tuple[Any, ...]) -> str:
    return target or (_string_value(_arg(args, 0)) if name.startswith("page.") else None) or ""


def _select_option(value: Any, kwargs: dict[str, Any]) -> SelectOption | None:
    if value is None:
        if not {"label", "value", "index"} & kwargs.keys():
            return None
        value = {key: kwargs.get(key) for key in ("label", "value", "index")}
    if isinstance(value, str):
        return SelectOption(value=value)
    if isinstance(value, int):
        return SelectOption(index=value)
    if isinstance(value, dict):
        return SelectOption(
            label=_string_value(value.get("label")),
            value=_string_value(value.get("value")),
            index=value.get("index") if isinstance(value.get("index"), int) else None,
        )
    return None


def _recorded_action_fields(
    action_type: ActionType,
    name: str,
    target: str | None,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    if action_type.is_web_action():
        fields["element_id"] = _element_id(name, target, args)

    value_index = _page_value_index(name, target)
    if action_type == ActionType.GOTO_URL:
        fields["url"] = _string_value(kwargs.get("url", _arg(args, 0)))
    elif action_type == ActionType.INPUT_TEXT:
        # Preserve the existing recorder privacy boundary: input values may be credentials,
        # so the typed action carries the required field without retaining the raw value.
        fields["text"] = ""
    elif action_type == ActionType.UPLOAD_FILE:
        fields["file_url"] = _string_value(kwargs.get("file_url", _arg(args, value_index)))
    elif action_type == ActionType.DOWNLOAD_FILE:
        fields["file_name"] = _string_value(kwargs.get("file_name", _arg(args, 0))) or "download_file"
        download_url = _string_value(kwargs.get("download_url", _arg(args, 1)))
        if download_url is not None:
            fields["download_url"] = download_url
    elif action_type == ActionType.SELECT_OPTION:
        option = _select_option(kwargs.get("value", _arg(args, value_index)), kwargs)
        if option is not None:
            fields["option"] = option
    elif action_type == ActionType.CHECKBOX:
        fields["is_checked"] = not name.endswith(".uncheck")
    elif action_type == ActionType.EXTRACT:
        prompt = kwargs.get("prompt", _arg(args, 0))
        if isinstance(prompt, str):
            fields["data_extraction_goal"] = prompt
        schema = kwargs.get("schema", _arg(args, 1))
        if schema is not None:
            fields["data_extraction_schema"] = schema
    elif action_type == ActionType.EXECUTE_JS:
        fields["js_code"] = _string_value(kwargs.get("expression", _arg(args, 0)))
    elif action_type == ActionType.KEYPRESS:
        keys = kwargs.get("keys", _arg(args, value_index))
        fields["keys"] = (
            [str(key) for key in keys] if isinstance(keys, list) else [str(keys)] if keys is not None else []
        )
        fields["hold"] = bool(kwargs.get("hold", False))
        if "duration" in kwargs:
            fields["duration"] = int(kwargs["duration"])
    elif action_type == ActionType.SCROLL:
        fields["scroll_x"] = kwargs.get("scroll_x", _arg(args, 0))
        fields["scroll_y"] = kwargs.get("scroll_y", _arg(args, 1))
    elif action_type == ActionType.MOVE:
        fields["x"] = kwargs.get("x", _arg(args, 0))
        fields["y"] = kwargs.get("y", _arg(args, 1))
    elif action_type == ActionType.DRAG:
        fields["start_x"] = kwargs.get("start_x", _arg(args, 0))
        fields["start_y"] = kwargs.get("start_y", _arg(args, 1))
        fields["path"] = kwargs.get("path", _arg(args, 2))
    elif action_type == ActionType.LEFT_MOUSE:
        fields["x"] = kwargs.get("x", _arg(args, 0))
        fields["y"] = kwargs.get("y", _arg(args, 1))
        fields["direction"] = kwargs.get("direction", _arg(args, 2))
    return {key: value for key, value in fields.items() if value is not None}


def _action_from_fields(
    action_type: ActionType,
    fields: dict[str, Any],
    *,
    warning: str,
) -> Action:
    # Import lazily: db.utils imports workflow models through schema conversion helpers.
    from skyvern.forge.sdk.db.utils import ACTION_TYPE_TO_CLASS

    action_class = ACTION_TYPE_TO_CLASS.get(action_type, Action)
    if action_class is Action:
        return Action(**fields)
    try:
        return action_class(**fields)
    except pydantic.ValidationError as exc:
        LOG.warning(
            warning,
            action_type=action_type,
            subclass=action_class.__name__,
            errors=exc.errors(),
        )
        return Action(**fields)


def recorded_action_from_payload(raw: dict[str, Any]) -> Action:
    action_type = ActionType(raw["action_type"])
    return _action_from_fields(
        action_type,
        raw,
        warning="Failed to instantiate masked recorded action subclass, falling back to base Action",
    )


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
        kwargs: dict[str, Any],
        description: str | None = None,
    ) -> Any:
        started = time.monotonic()
        # Input values may be credentials (incl. derived TOTP codes); never describe them.
        describe_args = () if action_type == ActionType.INPUT_TEXT else args
        common_fields = dict(
            action_type=action_type,
            status=ActionStatus.completed,
            action_order=len(self.actions),
            # A reader-facing prompt (page.extract/complete) is the action's own copy; prefer it over
            # the "page.method arg" form so the timeline reads as plain language even when the editor's
            # derived step is missing or stale and the UI falls back to this description.
            description=description if description is not None else _describe(name, target, describe_args),
            output={"code_line": _frame_user_line()},
        )
        action = _action_from_fields(
            action_type,
            {**common_fields, **_recorded_action_fields(action_type, name, target, args, kwargs)},
            warning="Failed to instantiate recorded action subclass, falling back to base Action",
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


def _wrap_recording_result(value: Any, recorder: _Recorder, selector: str | None) -> Any:
    if isinstance(value, list):
        return [_wrap_recording_result(item, recorder, selector) for item in value]
    if type(value).__module__.startswith("playwright.") and type(value).__name__ in _RECORDABLE_HANDLE_TYPE_NAMES:
        return RecordingLocator(value, recorder, selector)
    return value


def _wrap_call_result(value: Any, recorder: _Recorder, selector: str | None) -> Any:
    if inspect.isawaitable(value):

        async def resolve() -> Any:
            return _wrap_recording_result(await value, recorder, selector)

        return resolve()
    return _wrap_recording_result(value, recorder, selector)


class RecordingLocator:
    # Private worker-side hooks consumed by PlaywrightPageOperationBroker. The sandbox only
    # receives opaque markers, never this proxy instance.
    _skyvern_brokerable_handle = True

    def __init__(self, locator: Any, recorder: _Recorder, selector: str | None) -> None:
        self.__locator = locator
        self.__recorder = recorder
        self.__selector = selector

    def _skyvern_page_operation_argument(self) -> Any:
        return self.__locator

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
        if not callable(attr):
            return attr
        if action_type is None:

            def forwarded(*args: Any, **kwargs: Any) -> Any:
                return _wrap_call_result(attr(*args, **kwargs), self.__recorder, self.__selector)

            return forwarded

        async def recorded(*args: Any, **kwargs: Any) -> Any:
            return await self.__recorder.record(
                action_type, f"locator.{name}", self.__selector, lambda: attr(*args, **kwargs), args, kwargs
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
                ActionType.KEYPRESS, "keyboard.press", None, lambda: attr(*args, **kwargs), args, kwargs
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
        if not callable(attr):
            return attr
        if action_type is None:

            def forwarded(*args: Any, **kwargs: Any) -> Any:
                return _wrap_call_result(attr(*args, **kwargs), self.__recorder, _factory_selector(name, args))

            return forwarded
        record_prompt = name in _PROMPT_METHODS

        async def recorded(*args: Any, **kwargs: Any) -> Any:
            description: str | None = None
            if record_prompt:
                prompt = kwargs.get("prompt", args[0] if args else None)
                if isinstance(prompt, str) and prompt.strip():
                    description = " ".join(prompt.split())[:200]
            return await self.__recorder.record(
                action_type,
                f"page.{name}",
                None,
                lambda: attr(*args, **kwargs),
                args,
                kwargs,
                description=description,
            )

        return recorded


def json_safe_recorder_output(value: Any) -> Any:
    """Recursively replace leaked recorder proxies with a JSON-safe marker before a code block's
    output is registered. A raw RecordingLocator/RecordingKeyboard/RecordingPage reaching JSON
    serialization raises TypeError at the output-registration boundary, which drops the whole
    output payload and starves downstream evidence consumers.

    A leaked proxy is a generated-code defect with no meaningful serializable value, so it collapses
    to a type marker rather than its selector: a selector is only a lossy display fragment and can
    embed a resolved credential, which mask_secrets_in_data does not scrub out of a dict key."""
    if isinstance(value, (RecordingLocator, RecordingKeyboard, RecordingPage)):
        return f"<{type(value).__name__}>"
    if isinstance(value, dict):
        # Normalize keys too: json.dumps rejects a non-primitive key outright (it never consults
        # default=), so a proxy used as a mapping key would crash serialization all the same.
        return {json_safe_recorder_output(key): json_safe_recorder_output(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe_recorder_output(item) for item in value]
    return value
