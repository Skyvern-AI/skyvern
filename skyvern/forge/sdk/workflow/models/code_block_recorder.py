from __future__ import annotations

import asyncio
import inspect
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from os import PathLike, fspath
from types import FrameType
from typing import Any, Literal, cast

import pydantic
import structlog
from playwright.async_api import BrowserContext, Locator, Page

from skyvern.forge.sdk.db.datetime_utils import naive_utc_now
from skyvern.forge.sdk.db.id import generate_action_id
from skyvern.forge.sdk.workflow.models.credential_release import CredentialReleaseGuard
from skyvern.webeye.actions.action_types import ActionType
from skyvern.webeye.actions.actions import Action, ActionStatus, SelectOption
from skyvern.webeye.actions.handler_utils import strategy_aware_input
from skyvern.webeye.playwright_input import PlaywrightInputDefaults

LOG = structlog.get_logger()

_LOCATOR_STRATEGY_INPUT_OPTIONS = frozenset({"delay", "force", "no_wait_after", "text", "timeout", "value"})
_PAGE_STRATEGY_INPUT_OPTIONS = frozenset(
    {"delay", "force", "no_wait_after", "selector", "strict", "text", "timeout", "value"}
)


def _effective_playwright_timeout(defaults: PlaywrightInputDefaults, timeout: float | None) -> float:
    """Resolve one aggregate deadline while preserving explicit zero."""
    return defaults.timeout_ms if timeout is None else timeout


def _page_strategy_input_locator(
    page: Page,
    selector: str,
    strict: bool | None,
    defaults: PlaywrightInputDefaults,
) -> Locator:
    """Build the locator equivalent of Page.fill/type without changing context strictness."""
    locator = page.locator(selector)
    if strict is not None:
        return locator if strict else locator.first
    return locator if defaults.strict_selectors else locator.first


CODE_BLOCK_FILENAME = "<code_block>"
# full_code = "\nasync def wrapper(...):\n<user code from line 3>"; user line = frame line - 2
CODE_LINE_OFFSET = 2
MAX_RECORDED_ACTION_VALIDATION_ERROR_FIELDS = 20
PENDING_CALL_DELAY_SECONDS = 20.0
# Shared by persistence and Copilot projection. The measured attribute-heavy click fixture's
# first cause line ends at character 933, so 1000 retains it without forwarding the 8000-character
# capture budget into storage, the run UI, or model context.
RECORDED_FAILURE_RESPONSE_MAX_CHARS = 1000
RECORDED_FAILURE_CAPTURE_MAX_CHARS = 8000

# Kept in the OSS workflow runtime because the standalone CodeBlock runner image
# ships only `codeblock/`; its matching worker copy lives in failure_page_state.py.
COVERING_ELEMENT_SCRIPT = """el => {
  if (!(el instanceof Element)) return null;
  const rect = el.getBoundingClientRect();
  if (rect.width <= 0 || rect.height <= 0) return null;
  const x = rect.left + rect.width / 2, y = rect.top + rect.height / 2;
  const doc = el.ownerDocument;
  if (x < 0 || y < 0 || x >= doc.documentElement.clientWidth || y >= doc.documentElement.clientHeight) return null;
  const hit = doc.elementFromPoint(x, y);
  if (!hit || hit === el || el.contains(hit)) return null;
  return '<' + hit.tagName.toLowerCase() + (hit.id ? ' id=' + JSON.stringify(hit.id) : '') + (hit.getAttribute('class') ? ' class=' + JSON.stringify(hit.getAttribute('class')) : '') + '>';
}"""


def append_failure_page_state(
    reason: str,
    *,
    final_url: str | None = None,
    page_title: str | None = None,
    covering_element: str | None = None,
) -> str:
    """Append already-masked facts. Callers must redact before this bounded projection."""
    facts = [("Final URL", final_url), ("Page title", page_title), ("Covering element", covering_element)]
    return reason + "".join(f"\n{label}: {value[:1000]}" for label, value in facts if value)


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
    "press_sequentially": ActionType.INPUT_TEXT,
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


@dataclass(frozen=True, slots=True)
class PendingAction:
    call_name: str
    threshold_seconds: float
    code_line: int | None = None
    action_type: ActionType | None = None
    action_order: int | None = None


OnPendingAction = Callable[[PendingAction], None]


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
    try:
        tb = BaseException.__getattribute__(exc, "__traceback__")
        line: int | None = None
        while tb is not None:
            if tb.tb_frame.f_code.co_filename == CODE_BLOCK_FILENAME:
                line = max(tb.tb_lineno - CODE_LINE_OFFSET, 1)
            tb = tb.tb_next
        return line
    except BaseException:
        return None


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
        keys = kwargs.get("keys", _arg(args, 0))
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
        error_count = exc.error_count()
        error_fields = []
        # errors() materializes a dict per failure, so only call it when every entry fits. Retain
        # only loc/type because input/msg can echo the value that failed validation.
        if error_count <= MAX_RECORDED_ACTION_VALIDATION_ERROR_FIELDS:
            for error in exc.errors():
                field_path = ".".join(str(part) for part in error["loc"])
                error_fields.append((field_path, error["type"]))
        LOG.warning(
            warning,
            action_type=action_type,
            subclass=action_class.__name__,
            error_count=error_count,
            error_fields=error_fields,
            error_fields_truncated=error_count > MAX_RECORDED_ACTION_VALIDATION_ERROR_FIELDS,
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
    def __init__(
        self,
        on_action: OnAction | None = None,
        credential_release_guard: CredentialReleaseGuard | None = None,
        on_pending_action: OnPendingAction | None = None,
        strategy_aware_typing: bool = False,
        playwright_input_defaults: PlaywrightInputDefaults | None = None,
    ) -> None:
        self.actions: list[Action] = []
        self.last_exception: BaseException | None = None
        self.failed_locator: Locator | None = None
        self.failed_locator_exception: BaseException | None = None
        self.failure_operation_generation = 0
        self._on_action = on_action
        self._on_pending_action = on_pending_action
        self.credential_release_guard = credential_release_guard
        self.strategy_aware_typing = strategy_aware_typing
        self.playwright_input_defaults = playwright_input_defaults or PlaywrightInputDefaults()

    def _emit_pending_action(self, pending_action: PendingAction) -> None:
        if self._on_pending_action is None:
            return
        try:
            self._on_pending_action(pending_action)
        except Exception:
            LOG.warning(
                "Code block pending action sink failed",
                action_type=pending_action.action_type,
                call_name=pending_action.call_name,
            )

    def _arm_pending(self, pending_action: PendingAction) -> asyncio.TimerHandle | None:
        if self._on_pending_action is None:
            return None
        # A timer handle over a plain callback, never a task: the OTel asyncio instrumentor wraps
        # every scheduled coroutine, and a coroutine cancelled before its first step is dropped
        # inside that wrapper, which CPython reports as a "never awaited" RuntimeWarning. Cancelling
        # a handle is also synchronous, so the guarded call's finally never awaits and can no longer
        # swallow a cancellation aimed at the caller.
        return asyncio.get_running_loop().call_later(
            pending_action.threshold_seconds, self._emit_pending_action, pending_action
        )

    async def await_pending_aware(self, awaitable: Awaitable[Any], call_name: str) -> Any:
        if self._on_pending_action is None:
            return await awaitable
        pending_handle = self._arm_pending(
            PendingAction(
                call_name=call_name,
                threshold_seconds=PENDING_CALL_DELAY_SECONDS,
                code_line=_frame_user_line(),
            )
        )
        if pending_handle is None:
            return await awaitable
        try:
            return await awaitable
        finally:
            pending_handle.cancel()

    async def enforce_credential_release(
        self,
        target: Any,
        name: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> None:
        if self.credential_release_guard is not None:
            await self.credential_release_guard.enforce(target, name, args, kwargs)

    def begin_failure_operation(self) -> int:
        self.failure_operation_generation += 1
        self.failed_locator_exception = None
        self.failed_locator = None
        return self.failure_operation_generation

    async def record(
        self,
        action_type: ActionType,
        name: str,
        target: str | None,
        call: Callable[[], Awaitable[Any]],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        description: str | None = None,
        failure_locator: Locator | None = None,
    ) -> Any:
        generation = self.begin_failure_operation()
        started = time.monotonic()
        started_wall = naive_utc_now()
        code_line = _frame_user_line()
        action_order = len(self.actions)
        # Input values may be credentials (incl. derived TOTP codes); never describe them.
        describe_args = () if action_type == ActionType.INPUT_TEXT else args
        common_fields = dict(
            # Stable id assigned once so the streamed write and the end-of-block batch upsert the same row.
            action_id=generate_action_id(),
            action_type=action_type,
            status=ActionStatus.completed,
            action_order=action_order,
            # A reader-facing prompt (page.extract/complete) is the action's own copy; prefer it over
            # the "page.method arg" form so the timeline reads as plain language even when the editor's
            # derived step is missing or stale and the UI falls back to this description.
            description=description if description is not None else _describe(name, target, describe_args),
            output={"code_line": code_line},
        )
        action = _action_from_fields(
            action_type,
            {**common_fields, **_recorded_action_fields(action_type, name, target, args, kwargs)},
            warning="Failed to instantiate recorded action subclass, falling back to base Action",
        )
        pending_handle = self._arm_pending(
            PendingAction(
                call_name=name,
                threshold_seconds=PENDING_CALL_DELAY_SECONDS,
                code_line=code_line,
                action_type=action_type,
                action_order=action_order,
            )
        )
        try:
            result = await call()
        except BaseException as exc:
            action.status = ActionStatus.failed
            # Generous rather than tight: the persistence path masks secrets by exact match, so a
            # tight bound here could split one into an unmatched fragment. It cannot be unbounded --
            # redact_codeblock_parameter_values counts disclosure characters across the whole payload
            # and returns a replacement string past its budget, which would drop the row entirely.
            # A user-defined __str__ can raise or return a non-str. Unguarded, that replaces the
            # browser's failure with its own and skips both last_exception and the re-raise below,
            # so the run would lose the fault this line exists to report.
            try:
                captured = str(exc)[:RECORDED_FAILURE_CAPTURE_MAX_CHARS]
            except BaseException:
                captured = ""
            action.response = captured or type(exc).__name__
            self.last_exception = exc
            if generation == self.failure_operation_generation:
                self.failed_locator_exception = exc
                self.failed_locator = failure_locator
            raise
        finally:
            if pending_handle is not None:
                pending_handle.cancel()
            duration_ms = int((time.monotonic() - started) * 1000)
            action.started_at = started_wall
            action.finished_at = naive_utc_now()
            if isinstance(action.output, dict):
                action.output["duration_ms"] = duration_ms
            self.actions.append(action)
            if self._on_action is not None:
                try:
                    await self._on_action(action)
                except Exception:
                    LOG.warning("Code block action sink failed", action_type=action_type)
        return result


def _wrap_recording_result(value: Any, recorder: _Recorder, selector: str | None) -> Any:
    if isinstance(value, list):
        return [_wrap_recording_result(item, recorder, selector) for item in value]
    if type(value).__module__.startswith("playwright.") and type(value).__name__ in _RECORDABLE_HANDLE_TYPE_NAMES:
        return RecordingLocator(value, recorder, selector)
    return value


def _wrap_call_result(value: Any, recorder: _Recorder, selector: str | None, call_name: str) -> Any:
    if inspect.isawaitable(value):

        async def resolve() -> Any:
            return _wrap_recording_result(await recorder.await_pending_aware(value, call_name), recorder, selector)

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

    @property
    def last(self) -> RecordingLocator:
        return RecordingLocator(self.__locator.last, self.__recorder, self.__selector)

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
                generation = self.__recorder.begin_failure_operation()
                value = _wrap_call_result(attr(*args, **kwargs), self.__recorder, self.__selector, f"locator.{name}")
                if not inspect.isawaitable(value):
                    return value

                async def resolve() -> Any:
                    try:
                        return await value
                    except Exception as exc:
                        if generation == self.__recorder.failure_operation_generation:
                            self.__recorder.failed_locator_exception = exc
                            self.__recorder.failed_locator = self.__locator
                        raise

                return resolve()

            return forwarded

        async def recorded(*args: Any, **kwargs: Any) -> Any:
            async def call() -> Any:
                if self.__recorder.strategy_aware_typing and name in ("fill", "type"):
                    inspect.signature(attr).bind(*args, **kwargs)
                await self.__recorder.enforce_credential_release(self.__locator, f"locator.{name}", args, kwargs)
                if (
                    self.__recorder.strategy_aware_typing
                    and name in ("fill", "type")
                    and hasattr(self.__locator, "page")
                    and kwargs.keys() <= _LOCATOR_STRATEGY_INPUT_OPTIONS
                ):
                    value = kwargs.get("value" if name == "fill" else "text", args[0] if args else None)
                    if isinstance(value, str):
                        authored_timeout = kwargs.get("timeout")
                        if authored_timeout is None or isinstance(authored_timeout, int | float):
                            timeout = _effective_playwright_timeout(
                                self.__recorder.playwright_input_defaults,
                                authored_timeout,
                            )
                            force = kwargs.get("force")
                            delay = kwargs.get("delay")
                            no_wait_after = kwargs.get("no_wait_after")
                            if force is None and delay is None and no_wait_after is None:
                                return await strategy_aware_input(
                                    self.__locator,
                                    value,
                                    clear=name == "fill",
                                    timeout=timeout,
                                )
                            return await strategy_aware_input(
                                self.__locator,
                                value,
                                clear=name == "fill",
                                timeout=timeout,
                                force=force,
                                delay=delay,
                                no_wait_after=no_wait_after,
                            )
                return await attr(*args, **kwargs)

            return await self.__recorder.record(
                action_type, f"locator.{name}", self.__selector, call, args, kwargs, failure_locator=self.__locator
            )

        return recorded


class RecordingKeyboard:
    def __init__(self, keyboard: Any, recorder: _Recorder, page: Any = None) -> None:
        self.__keyboard = keyboard
        self.__recorder = recorder
        self.__page = page

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self.__keyboard, name)
        if not callable(attr):
            return attr
        if name in ("type", "insert_text"):

            async def guarded(*args: Any, **kwargs: Any) -> Any:
                self.__recorder.begin_failure_operation()

                async def call() -> Any:
                    await self.__recorder.enforce_credential_release(self.__page, f"keyboard.{name}", args, kwargs)
                    return await attr(*args, **kwargs)

                return await self.__recorder.await_pending_aware(call(), f"keyboard.{name}")

            return guarded
        if name != "press":

            def forwarded(*args: Any, **kwargs: Any) -> Any:
                self.__recorder.begin_failure_operation()
                return _wrap_call_result(attr(*args, **kwargs), self.__recorder, None, f"keyboard.{name}")

            return forwarded

        async def recorded(*args: Any, **kwargs: Any) -> Any:
            return await self.__recorder.record(
                ActionType.KEYPRESS, "keyboard.press", None, lambda: attr(*args, **kwargs), args, kwargs
            )

        return recorded


class RecordingBrowserContext:
    def __init__(self, context: BrowserContext, recorder: _Recorder) -> None:
        self.__context = context
        self.__recorder = recorder

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self.__context, name)
        if name != "set_default_timeout" or not callable(attr):
            return attr

        def set_default_timeout(timeout: float) -> None:
            attr(timeout)
            self.__recorder.playwright_input_defaults.set_context_timeout(timeout)

        return set_default_timeout


class RecordingPage:
    """Proxy that records mapped Playwright calls as Actions.

    Recordings are telemetry, not a tamper-proof audit trail.
    """

    def __init__(
        self,
        page: Any,
        on_action: OnAction | None = None,
        credential_release_guard: CredentialReleaseGuard | None = None,
        on_pending_action: OnPendingAction | None = None,
        strategy_aware_typing: bool = False,
        playwright_input_defaults: PlaywrightInputDefaults | None = None,
    ) -> None:
        self.__page = page
        self.__recorder = _Recorder(
            on_action,
            credential_release_guard,
            on_pending_action,
            strategy_aware_typing=strategy_aware_typing,
            playwright_input_defaults=playwright_input_defaults,
        )

    def recorded_actions(self) -> list[Action]:
        return list(self.__recorder.actions)

    def last_recorded_exception(self) -> BaseException | None:
        return self.__recorder.last_exception

    def failure_locator(self, exception: BaseException) -> Locator | None:
        return self.__recorder.failed_locator if self.__recorder.failed_locator_exception is exception else None

    def _brokered_default_timeout(self, scope: Literal["page", "context"]) -> float | None:
        """Return the trusted timeout that a secure-runner block must restore."""
        if scope == "page":
            return self.__recorder.playwright_input_defaults.page_timeout_ms
        return self.__recorder.playwright_input_defaults.context_timeout_ms

    def _restore_brokered_default_timeout(
        self,
        scope: Literal["page", "context"],
        timeout: float | None,
    ) -> None:
        """Restore the pre-block override, including Page inheritance from BrowserContext."""
        if scope == "page":
            # The generated public wrapper forwards None to Playwright's supported optional
            # timeout setting, clearing a Page override without inspecting implementation state.
            setter = cast(Callable[[float | None], None], self.__page.set_default_timeout)
            setter(timeout)
            self.__recorder.playwright_input_defaults.restore_page_timeout(timeout)
            return
        if timeout is None:
            raise ValueError("context default timeout cannot inherit from another scope")
        self.__page.context.set_default_timeout(timeout)
        self.__recorder.playwright_input_defaults.set_context_timeout(timeout)

    def _set_brokered_default_timeout(self, scope: Literal["page", "context"], timeout: float) -> None:
        """Apply a synchronous Playwright setter received over the secure-runner transport."""
        if scope == "page":
            self.__page.set_default_timeout(timeout)
            self.__recorder.playwright_input_defaults.set_page_timeout(timeout)
            return
        if scope == "context":
            self.__page.context.set_default_timeout(timeout)
            self.__recorder.playwright_input_defaults.set_context_timeout(timeout)
            return
        raise ValueError(f"unsupported default-timeout scope: {scope}")

    def _supports_brokered_default_timeout(self) -> bool:
        # Snapshot/restore rides on playwright_input_defaults, which every recorder always has;
        # it is independent of the typing strategy, so the setter must not be gated on it.
        return True

    def locator(self, selector: str, **kwargs: Any) -> RecordingLocator:
        return RecordingLocator(self.__page.locator(selector, **kwargs), self.__recorder, selector)

    @property
    def keyboard(self) -> RecordingKeyboard:
        return RecordingKeyboard(self.__page.keyboard, self.__recorder, page=self.__page)

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self.__page, name)
        if name == "context" and self.__recorder.strategy_aware_typing:
            return RecordingBrowserContext(attr, self.__recorder)
        if name == "set_default_timeout" and self.__recorder.strategy_aware_typing and callable(attr):

            def set_default_timeout(timeout: float) -> None:
                attr(timeout)
                self.__recorder.playwright_input_defaults.set_page_timeout(timeout)

            return set_default_timeout
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
                self.__recorder.begin_failure_operation()
                return _wrap_call_result(
                    attr(*args, **kwargs), self.__recorder, _factory_selector(name, args), f"page.{name}"
                )

            return forwarded
        record_prompt = name in _PROMPT_METHODS

        async def recorded(*args: Any, **kwargs: Any) -> Any:
            description: str | None = None
            if record_prompt:
                prompt = kwargs.get("prompt", args[0] if args else None)
                if isinstance(prompt, str) and prompt.strip():
                    description = " ".join(prompt.split())[:200]

            async def call() -> Any:
                if self.__recorder.strategy_aware_typing and name in ("fill", "type"):
                    inspect.signature(attr).bind(*args, **kwargs)
                await self.__recorder.enforce_credential_release(self.__page, f"page.{name}", args, kwargs)
                if (
                    self.__recorder.strategy_aware_typing
                    and name in ("fill", "type")
                    and kwargs.keys() <= _PAGE_STRATEGY_INPUT_OPTIONS
                ):
                    selector = kwargs.get("selector", args[0] if args else None)
                    value_index = 1
                    value = kwargs.get("value" if name == "fill" else "text", _arg(args, value_index))
                    strict = kwargs.get("strict")
                    authored_timeout = kwargs.get("timeout")
                    if (
                        isinstance(selector, str)
                        and isinstance(value, str)
                        and strict in (None, False, True)
                        and (authored_timeout is None or isinstance(authored_timeout, int | float))
                    ):
                        locator = _page_strategy_input_locator(
                            self.__page,
                            selector,
                            strict,
                            self.__recorder.playwright_input_defaults,
                        )
                        timeout = _effective_playwright_timeout(
                            self.__recorder.playwright_input_defaults,
                            authored_timeout,
                        )
                        force = kwargs.get("force")
                        delay = kwargs.get("delay")
                        no_wait_after = kwargs.get("no_wait_after")
                        if force is None and delay is None and no_wait_after is None:
                            return await strategy_aware_input(
                                locator,
                                value,
                                clear=name == "fill",
                                timeout=timeout,
                            )
                        return await strategy_aware_input(
                            locator,
                            value,
                            clear=name == "fill",
                            timeout=timeout,
                            force=force,
                            delay=delay,
                            no_wait_after=no_wait_after,
                        )
                return await attr(*args, **kwargs)

            return await self.__recorder.record(
                action_type,
                f"page.{name}",
                None,
                call,
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
