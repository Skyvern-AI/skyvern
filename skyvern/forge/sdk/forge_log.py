import logging

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

    def __call__(self, logger: logging.Logger, name: str, event_dict: EventDict) -> str:
        file_section = event_dict.pop("file", "")
        file_section_colored = f"\x1b[90m{file_section}\x1b[0m" if file_section else ""
        rendered = super().__call__(logger, name, event_dict)
        first_bracket = rendered.find("]")

        if first_bracket != -1:
            return rendered[: first_bracket + 1] + f" {file_section_colored}" + rendered[first_bracket + 1 :]
        else:
            return f"{file_section_colored} {rendered}"


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
            # structlog.processors.dict_tracebacks,
            structlog.processors.format_exc_info,
        ]
        + additional_processors
        + [skyvern_logs_processor, renderer],
    )
    uvicorn_error = logging.getLogger("uvicorn.error")
    uvicorn_error.disabled = True
    uvicorn_access = logging.getLogger("uvicorn.access")
    uvicorn_access.disabled = True
