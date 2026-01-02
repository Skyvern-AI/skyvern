import logging
from types import TracebackType

import structlog
from structlog.typing import EventDict

from skyvern._version import __version__
from skyvern.config import settings
from skyvern.forge.sdk.core import skyvern_context

LOGGING_LEVEL_MAP: dict[str, int] = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


def add_kv_pairs_to_msg(logger: logging.Logger, method_name: str, event_dict: EventDict) -> EventDict:
    """
    A custom processor to add key-value pairs to the 'msg' field.
    """
    # Add context to the log
    context = skyvern_context.current()
    if context:
        if context.request_id:
            event_dict["request_id"] = context.request_id
        if context.organization_id:
            event_dict["organization_id"] = context.organization_id
        if context.organization_name:
            event_dict["organization_name"] = context.organization_name
        if context.step_id:
            event_dict["step_id"] = context.step_id
        if context.task_id:
            event_dict["task_id"] = context.task_id
        if context.run_id:
            event_dict["run_id"] = context.run_id
        if context.workflow_id:
            event_dict["workflow_id"] = context.workflow_id
        if context.workflow_run_id:
            event_dict["workflow_run_id"] = context.workflow_run_id
        if context.workflow_permanent_id:
            event_dict["workflow_permanent_id"] = context.workflow_permanent_id
        if context.task_v2_id:
            event_dict["task_v2_id"] = context.task_v2_id
        if context.browser_session_id:
            event_dict["browser_session_id"] = context.browser_session_id
        if context.browser_container_ip:
            event_dict["browser_container_ip"] = context.browser_container_ip
        if context.browser_container_task_arn:
            event_dict["browser_container_task_arn"] = context.browser_container_task_arn

    # Add env to the log
    event_dict["env"] = settings.ENV
    event_dict["version"] = __version__

    if method_name not in ["info", "warning", "error", "critical", "exception"]:
        # Only modify the log for these log levels
        return event_dict

    # Assuming 'event' or 'msg' is the field to update
    msg_field = event_dict.get("msg", "")

    # Add key-value pairs
    kv_pairs = {k: v for k, v in event_dict.items() if k not in ["msg", "timestamp", "level"]}
    if kv_pairs:
        additional_info = ", ".join(f"{k}={v}" for k, v in kv_pairs.items())
        msg_field += f" | {additional_info}"

    event_dict["msg"] = msg_field

    return event_dict


def skyvern_logs_processor(logger: logging.Logger, method_name: str, event_dict: EventDict) -> EventDict:
    """
    A custom processor to add skyvern logs to the context
    """
    if method_name not in ["info", "warning", "error", "critical", "exception"]:
        return event_dict

    context = skyvern_context.current()
    if context:
        log_entry = dict(event_dict)
        context.log.append(log_entry)

    return event_dict


def add_filename_section(logger: logging.Logger, method_name: str, event_dict: EventDict) -> EventDict:
    """
    Add a fixed-width, bracketed filename:lineno section after the log level for console logs.
    """
    filename = event_dict.get("filename", "")
    lineno = event_dict.get("lineno", "")
    padded = f"[{filename:<30}:{lineno:<4}]" if filename else "[unknown        ]"
    event_dict["file"] = padded
    event_dict.pop("filename", None)
    event_dict.pop("lineno", None)
    return event_dict


class CustomConsoleRenderer(structlog.dev.ConsoleRenderer):
    """
    Show the bracketed filename:lineno section after the log level for console logs, and
    colorize it.
    """

    def __init__(self) -> None:
        super().__init__(sort_keys=False)

    def __call__(self, logger: logging.Logger, name: str, event_dict: EventDict) -> str:
        file_section = event_dict.pop("file", "")
        file_section_colored = f"\x1b[90m{file_section}\x1b[0m" if file_section else ""
        rendered = super().__call__(logger, name, event_dict)
        first_bracket = rendered.find("]")

        if first_bracket != -1:
            return rendered[: first_bracket + 1] + f" {file_section_colored}" + rendered[first_bracket + 1 :]
        else:
            return f"{file_section_colored} {rendered}"


def add_error_processor(logger: logging.Logger, method_name: str, event_dict: EventDict) -> EventDict:
    """
    A custom processor extending error logs with additional info
    """
    import sys  # noqa: PLC0415

    exc_info = event_dict.get("exc_info")

    if exc_info:
        if exc_info is True:
            exc_info = sys.exc_info()

        if isinstance(exc_info, tuple) and len(exc_info) >= 2:
            exc_type = exc_info[0]
            exc_traceback: TracebackType | None = exc_info[2] if len(exc_info) >= 3 else None

            if exc_type is not None:
                # Get the fully qualified exception name (module.ClassName)
                error_type = (
                    f"{exc_type.__module__}.{exc_type.__name__}"
                    if hasattr(exc_type, "__module__")
                    else exc_type.__name__
                )
                event_dict["error_type"] = error_type

                # Categorize the exception
                category = _categorize_exception(exc_type, exc_type.__name__)
                event_dict["error_category"] = category

                # Generate exception hash from stack trace (stable identifier)
                if exc_traceback is not None:
                    exc_hash = _generate_exception_hash(exc_type, exc_traceback)
                    event_dict["exception_hash"] = exc_hash

    return event_dict


def _generate_exception_hash(exc_type: type, tb: TracebackType) -> str:
    """
    Generate a stable hash for an exception based on:
    - Exception type
    - Stack trace (filename, line number, function name)

    Excludes dynamic data like error messages to ensure the same
    error from the same location always produces the same hash.
    """
    import hashlib  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    hasher = hashlib.sha256()

    hasher.update(f"{exc_type.__module__}.{exc_type.__name__}".encode())

    current_tb: TracebackType | None = tb
    while current_tb is not None:
        frame = current_tb.tb_frame
        code = frame.f_code

        filename = Path(code.co_filename).name
        lineno = current_tb.tb_lineno
        func_name = code.co_name
        hasher.update(f"{filename}:{lineno}:{func_name}".encode())

        current_tb = current_tb.tb_next

    return hasher.hexdigest()[:16]


def _categorize_exception(exc_type: type, exc_name: str) -> str:
    """
    Categorize an exception into TRANSIENT, BUG, or ERROR.

    TRANSIENT: Network/IO errors that might succeed on retry
    BUG: Programming errors indicating bugs
    ERROR: Everything else
    """
    # Check if it's a subclass of known exception types
    # TRANSIENT - IO and network related errors
    transient_exceptions = (
        IOError,
        OSError,
        ConnectionError,
        TimeoutError,
        ConnectionRefusedError,
        ConnectionAbortedError,
        ConnectionResetError,
        BrokenPipeError,
    )

    # BUG - Programming errors that indicate bugs
    bug_exceptions = (
        ZeroDivisionError,
        AttributeError,
        TypeError,
        KeyError,
        IndexError,
        NameError,
        AssertionError,
        NotImplementedError,
        RecursionError,
        UnboundLocalError,
        IndentationError,
        SyntaxError,
    )

    # Check for common HTTP/network library exceptions by name
    # (to avoid import dependencies)
    transient_patterns = [
        "HTTPError",
        "RequestException",
        "Timeout",
        "ConnectionError",
        "ConnectTimeout",
        "ReadTimeout",
        "ProxyError",
        "SSLError",
        "ChunkedEncodingError",
        "ContentDecodingError",
        "StreamConsumedError",
        "RetryError",
        "MaxRetryError",
        "URLError",
        "ProtocolError",
    ]

    # Check if exception is a subclass of transient exceptions
    try:
        if issubclass(exc_type, transient_exceptions):
            return "TRANSIENT"
    except TypeError:
        pass

    # Check if exception is a subclass of bug exceptions
    try:
        if issubclass(exc_type, bug_exceptions):
            return "BUG"
    except TypeError:
        pass

    # Check exception name against patterns
    for pattern in transient_patterns:
        if pattern in exc_name:
            return "TRANSIENT"

    # Default to ERROR for everything else
    return "ERROR"


def setup_logger() -> None:
    """
    Setup the logger with the specified format
    """
    # logging.config.dictConfig(logging_config)
    renderer = structlog.processors.JSONRenderer() if settings.JSON_LOGGING else CustomConsoleRenderer()
    additional_processors = (
        [
            structlog.processors.EventRenamer("msg"),
            add_kv_pairs_to_msg,
            structlog.processors.CallsiteParameterAdder(
                {
                    structlog.processors.CallsiteParameter.PATHNAME,
                    structlog.processors.CallsiteParameter.FILENAME,
                    structlog.processors.CallsiteParameter.MODULE,
                    structlog.processors.CallsiteParameter.FUNC_NAME,
                    structlog.processors.CallsiteParameter.LINENO,
                }
            ),
        ]
        if settings.JSON_LOGGING
        else [
            structlog.processors.CallsiteParameterAdder(
                {
                    structlog.processors.CallsiteParameter.FILENAME,
                    structlog.processors.CallsiteParameter.LINENO,
                }
            ),
            add_filename_section,
        ]
    )
    LOG_LEVEL_VAL = LOGGING_LEVEL_MAP.get(settings.LOG_LEVEL, logging.INFO)

    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(LOG_LEVEL_VAL),
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            add_error_processor,
            structlog.processors.format_exc_info,
        ]
        + additional_processors
        + [skyvern_logs_processor, renderer],
    )
    uvicorn_error = logging.getLogger("uvicorn.error")
    uvicorn_error.disabled = True
    uvicorn_access = logging.getLogger("uvicorn.access")
    uvicorn_access.disabled = True
