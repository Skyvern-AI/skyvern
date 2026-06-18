from __future__ import annotations

import inspect
import ipaddress
import sys
import time
from types import FrameType
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

import structlog

from skyvern.config import settings
from skyvern.forge.sdk.workflow.exceptions import InsecureCodeDetected
from skyvern.utils.url_validators import is_blocked_host
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
    "wait_for_timeout": ActionType.WAIT,
}
# Playwright navigation APIs return raw Response objects with frame/page escape hatches.
# Code blocks only need the action side effect, so suppress these returns after recording.
_PAGE_ACTIONS_WITH_RAW_BROWSER_RESULTS = frozenset({"goto", "go_back", "go_forward", "reload"})
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
_SAFE_PAGE_METHODS = frozenset({"content", "title", "wait_for_load_state", "wait_for_url"})
_SAFE_LOCATOR_METHODS = frozenset(
    {
        "all_inner_texts",
        "all_text_contents",
        "bounding_box",
        "count",
        "get_attribute",
        "inner_html",
        "inner_text",
        "input_value",
        "is_checked",
        "is_disabled",
        "is_editable",
        "is_enabled",
        "is_visible",
        "text_content",
        "wait_for",
    }
)
_SAFE_DOWNLOAD_METHODS = frozenset({"failure", "path"})
_SAFE_DOWNLOAD_PROPERTIES = frozenset({"suggested_filename", "url"})
_DENIED_DOWNLOAD_ATTRIBUTES = frozenset({"page"})

OnAction = Callable[[Action], Awaitable[None]]

_DENIED_PAGE_ATTRIBUTES = frozenset(
    {
        "add_init_script",
        "context",
        "evaluate",
        "evaluate_handle",
        "expose_binding",
        "expose_function",
        "frame",
        "frame_locator",
        "frames",
        "main_frame",
        "request",
        "route",
    }
)


class CodeBlockSecret:
    __slots__ = ("__value",)
    __hash__ = None  # type: ignore[assignment]

    def __init__(self, value: str) -> None:
        self.__value = value

    def __str__(self) -> str:
        return "*****"

    def __repr__(self) -> str:
        return "*****"

    def __bool__(self) -> bool:
        return bool(self.__value)

    def __eq__(self, other: object) -> bool:
        # Always false: no comparison-based exfiltration path.
        return False

    def _reveal_for_input_action(self) -> str:
        return self.__value


def _reveal_code_block_secret(value: Any) -> Any:
    if isinstance(value, CodeBlockSecret):
        return value._reveal_for_input_action()
    return value


def _resolve_input_secrets(value: Any) -> Any:
    if isinstance(value, tuple):
        return tuple(_resolve_input_secrets(item) for item in value)
    if isinstance(value, list):
        return [_resolve_input_secrets(item) for item in value]
    if isinstance(value, dict):
        return {key: _resolve_input_secrets(item) for key, item in value.items()}
    return _reveal_code_block_secret(value)


def _contains_code_block_secret(value: Any) -> bool:
    if isinstance(value, CodeBlockSecret):
        return True
    if isinstance(value, (tuple, list, set)):
        return any(_contains_code_block_secret(item) for item in value)
    if isinstance(value, dict):
        return any(_contains_code_block_secret(item) for pair in value.items() for item in pair)
    return False


def _action_call_args(
    action_type: ActionType, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> tuple[tuple[Any, ...], dict[str, Any]]:
    if action_type != ActionType.INPUT_TEXT:
        if _contains_code_block_secret(args) or _contains_code_block_secret(kwargs):
            raise InsecureCodeDetected("CodeBlockSecret can only be used in fill/type (INPUT_TEXT) actions")
        return args, kwargs
    return _resolve_input_secrets(args), _resolve_input_secrets(kwargs)


def _origin(url: str | None) -> tuple[str, str, int | None] | None:
    if not url:
        return None
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.hostname:
        return None
    return (parsed.scheme.lower(), parsed.hostname.lower(), parsed.port)


def _is_loopback_fixture_host(host: str) -> bool:
    normalized = host[1:-1].lower() if host.startswith("[") and host.endswith("]") else host.lower()
    if normalized == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(normalized)
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
    except ValueError:
        return False
    return ip.is_loopback


def _host_allowed_for_loopback_fixture(host: str) -> bool:
    bare = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    candidate_forms = {host.lower(), bare.lower()}
    try:
        ip = ipaddress.ip_address(bare)
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        candidate_forms.add(str(ip).lower())
    except ValueError:
        pass
    allowed_hosts = {allowed.lower() for allowed in settings.ALLOWED_HOSTS}
    return bool(candidate_forms & allowed_hosts)


def _parse_browser_numeric_host(host: str) -> ipaddress.IPv4Address | None:
    """Parse legacy numeric IPv4 forms that browsers normalize but ipaddress rejects."""
    if not host or ":" in host:
        return None
    parts = host.split(".")
    if len(parts) > 4:
        return None

    parsed_parts: list[int] = []
    for part in parts:
        if not part:
            return None
        lowered = part.lower()
        try:
            if lowered.startswith("0x"):
                value = int(lowered[2:], 16)
            elif len(lowered) > 1 and lowered.startswith("0"):
                if any(ch not in "01234567" for ch in lowered):
                    return None
                value = int(lowered, 8)
            elif lowered.isdigit():
                value = int(lowered, 10)
            else:
                return None
        except ValueError:
            return None
        parsed_parts.append(value)

    if any(part > 255 for part in parsed_parts[:-1]):
        return None
    tail_max = (256 ** (5 - len(parsed_parts))) - 1
    if parsed_parts[-1] > tail_max:
        return None

    address = 0
    for parsed_part in parsed_parts[:-1]:
        address = (address * 256) + parsed_part
    address = (address * (256 ** (5 - len(parsed_parts)))) + parsed_parts[-1]
    if address > 0xFFFFFFFF:
        return None
    return ipaddress.IPv4Address(address)


def _parse_host_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    bare = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    try:
        ip = ipaddress.ip_address(bare)
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            return ip.ipv4_mapped
        return ip
    except ValueError:
        return _parse_browser_numeric_host(bare.lower())


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return ip.is_private or ip.is_link_local or ip.is_loopback or ip.is_multicast or ip.is_reserved


def _validate_goto_target(target_url: str, current_url: str | None) -> None:
    if not target_url.strip():
        raise InsecureCodeDetected("Code block page.goto blocked empty target")
    target = urlparse(target_url)
    if not target.scheme and not target.netloc:
        return
    if target.scheme not in {"http", "https"}:
        raise InsecureCodeDetected(f"Code block page.goto blocked non-http target: {target_url}")
    if not target.hostname:
        raise InsecureCodeDetected(f"Code block page.goto blocked target: {target_url}")

    target_ip = _parse_host_ip(target.hostname)
    if target_ip is not None and _is_blocked_ip(target_ip):
        # ALLOWED_HOSTS is a loopback-only fixture escape hatch here; private/link-local
        # non-loopback addresses stay blocked even when globally allowlisted.
        if _is_loopback_fixture_host(target.hostname) and (
            _origin(target_url) == _origin(current_url) or _host_allowed_for_loopback_fixture(target.hostname)
        ):
            return
        raise InsecureCodeDetected(f"Code block page.goto blocked target: {target_url}")

    if not is_blocked_host(target.hostname):
        return
    if _is_loopback_fixture_host(target.hostname) and (
        _origin(target_url) == _origin(current_url) or _host_allowed_for_loopback_fixture(target.hostname)
    ):
        return
    raise InsecureCodeDetected(f"Code block page.goto blocked target: {target_url}")


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
    ) -> Any:
        started = time.monotonic()
        # Input values may be credentials (incl. derived TOTP codes); never describe them.
        describe_args = () if action_type == ActionType.INPUT_TEXT else args
        action = Action(
            action_type=action_type,
            status=ActionStatus.completed,
            action_order=len(self.actions),
            description=_describe(name, target, describe_args),
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

    @property
    def last(self) -> RecordingLocator:
        return RecordingLocator(self.__locator.last, self.__recorder, self.__selector)

    def nth(self, index: int) -> RecordingLocator:
        return RecordingLocator(self.__locator.nth(index), self.__recorder, self.__selector)

    async def all(self) -> list[RecordingLocator]:
        locators = self.__locator.all()
        if inspect.isawaitable(locators):
            locators = await locators
        return [RecordingLocator(locator, self.__recorder, self.__selector) for locator in locators]

    def __getattr__(self, name: str) -> Any:
        action_type = _LOCATOR_ACTION_MAP.get(name)
        if name not in _LOCATOR_FACTORY_METHODS and name not in _SAFE_LOCATOR_METHODS and action_type is None:
            raise InsecureCodeDetected(f"Code block locator.{name} is not available in the restricted page API")
        attr = getattr(self.__locator, name)
        if name in _LOCATOR_FACTORY_METHODS and callable(attr):

            def factory(*args: Any, **kwargs: Any) -> RecordingLocator:
                return RecordingLocator(attr(*args, **kwargs), self.__recorder, _factory_selector(name, args))

            return factory
        if action_type is None:
            if name in _SAFE_LOCATOR_METHODS and callable(attr):
                return attr
            raise InsecureCodeDetected(f"Code block locator.{name} is not available in the restricted page API")
        if not callable(attr):
            raise InsecureCodeDetected(f"Code block locator.{name} is not available in the restricted page API")

        async def recorded(*args: Any, **kwargs: Any) -> Any:
            call_args, call_kwargs = _action_call_args(action_type, args, kwargs)
            return await self.__recorder.record(
                action_type, f"locator.{name}", self.__selector, lambda: attr(*call_args, **call_kwargs), args
            )

        return recorded


class RecordingDownload:
    def __init__(self, download: Any) -> None:
        self.__download = download

    def __getattr__(self, name: str) -> Any:
        if name in _DENIED_DOWNLOAD_ATTRIBUTES or name.startswith("_"):
            raise InsecureCodeDetected(f"Code block download.{name} is not available in the restricted download API")
        if name in _SAFE_DOWNLOAD_PROPERTIES:
            return getattr(self.__download, name)
        if name in _SAFE_DOWNLOAD_METHODS:
            attr = getattr(self.__download, name)
            if callable(attr):
                return attr
        raise InsecureCodeDetected(f"Code block download.{name} is not available in the restricted download API")


class RecordingDownloadContext:
    def __init__(self, context_manager: Any) -> None:
        self.__context_manager = context_manager
        self.__entered_context: Any = context_manager

    async def __aenter__(self) -> RecordingDownloadContext:
        entered = await self.__context_manager.__aenter__()
        self.__entered_context = entered if entered is not None else self.__context_manager
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> Any:
        return await self.__context_manager.__aexit__(exc_type, exc, traceback)

    @property
    def value(self) -> Awaitable[RecordingDownload]:
        async def wrapped_value() -> RecordingDownload:
            value = self.__entered_context.value
            download = await value if inspect.isawaitable(value) else value
            return RecordingDownload(download)

        return wrapped_value()


class RecordingKeyboard:
    def __init__(self, keyboard: Any, recorder: _Recorder) -> None:
        self.__keyboard = keyboard
        self.__recorder = recorder

    def __getattr__(self, name: str) -> Any:
        if name != "press":
            raise InsecureCodeDetected(f"Code block keyboard.{name} is not available in the restricted page API")
        attr = getattr(self.__keyboard, name)
        if not callable(attr):
            raise InsecureCodeDetected(f"Code block keyboard.{name} is not available in the restricted page API")

        async def recorded(*args: Any, **kwargs: Any) -> Any:
            return await self.__recorder.record(
                ActionType.KEYPRESS, "keyboard.press", None, lambda: attr(*args, **kwargs), args
            )

        return recorded


class RecordingPage:
    """Proxy around a playwright page that records mapped calls as Actions.

    User code receives this restricted surface, while block execution keeps the
    raw Playwright page for internal screenshot capture and persistence work.
    """

    def __init__(self, page: Any, on_action: OnAction | None = None) -> None:
        self.__page = page
        self.__recorder = _Recorder(on_action)

    def _recorded_actions(self) -> list[Action]:
        return list(self.__recorder.actions)

    def _last_recorded_exception(self) -> BaseException | None:
        return self.__recorder.last_exception

    def locator(self, selector: str, **kwargs: Any) -> RecordingLocator:
        return RecordingLocator(self.__page.locator(selector, **kwargs), self.__recorder, selector)

    def expect_download(self, *args: Any, **kwargs: Any) -> RecordingDownloadContext:
        return RecordingDownloadContext(self.__page.expect_download(*args, **kwargs))

    @property
    def keyboard(self) -> RecordingKeyboard:
        return RecordingKeyboard(self.__page.keyboard, self.__recorder)

    @property
    def url(self) -> str:
        return self.__page.url

    def __getattr__(self, name: str) -> Any:
        if name in _DENIED_PAGE_ATTRIBUTES:
            raise InsecureCodeDetected(f"Code block page.{name} is not available in the restricted page API")
        action_type = _LOCATOR_ACTION_MAP.get(name) or _PAGE_ACTION_MAP.get(name)
        if name not in _LOCATOR_FACTORY_METHODS and name not in _SAFE_PAGE_METHODS and action_type is None:
            raise InsecureCodeDetected(f"Code block page.{name} is not available in the restricted page API")
        attr = getattr(self.__page, name)
        if name in _LOCATOR_FACTORY_METHODS and callable(attr):

            def factory(*args: Any, **kwargs: Any) -> RecordingLocator:
                return RecordingLocator(attr(*args, **kwargs), self.__recorder, _factory_selector(name, args))

            return factory
        # Record direct page-level interactions (page.click/fill/press/...) with the same
        # redaction as the locator path, alongside navigation calls.
        if action_type is None:
            if name in _SAFE_PAGE_METHODS and callable(attr):
                return attr
            raise InsecureCodeDetected(f"Code block page.{name} is not available in the restricted page API")
        if not callable(attr):
            raise InsecureCodeDetected(f"Code block page.{name} is not available in the restricted page API")

        async def recorded(*args: Any, **kwargs: Any) -> Any:
            if name == "goto":
                target_url = args[0] if args else kwargs.get("url")
                if not isinstance(target_url, str):
                    raise InsecureCodeDetected("Code block page.goto requires a string URL")
                _validate_goto_target(target_url, self.__page.url)
            call_args, call_kwargs = _action_call_args(action_type, args, kwargs)
            result = await self.__recorder.record(
                action_type, f"page.{name}", None, lambda: attr(*call_args, **call_kwargs), args
            )
            if name in _PAGE_ACTIONS_WITH_RAW_BROWSER_RESULTS:
                return None
            return result

        return recorded
