import logging

import structlog
from structlog.typing import EventDict

from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.settings_manager import SettingsManager

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
        if context.task_id:
            event_dict["task_id"] = context.task_id
        if context.workflow_id:
            event_dict["workflow_id"] = context.workflow_id
        if context.workflow_run_id:
            event_dict["workflow_run_id"] = context.workflow_run_id

    # Add env to the log
    event_dict["env"] = SettingsManager.get_settings().ENV

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


def setup_logger() -> None:
    """
    Setup the logger with the specified format
    """
    # logging.config.dictConfig(logging_config)
    renderer = (
        structlog.processors.JSONRenderer()
        if SettingsManager.get_settings().JSON_LOGGING
        else structlog.dev.ConsoleRenderer()
    )
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
        if SettingsManager.get_settings().JSON_LOGGING
        else []
    )
    LOG_LEVEL_VAL = LOGGING_LEVEL_MAP.get(SettingsManager.get_settings().LOG_LEVEL, logging.INFO)

    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(LOG_LEVEL_VAL),
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            # structlog.processors.dict_tracebacks,
            structlog.processors.format_exc_info,
        ]
        + additional_processors
        + [renderer],
    )
    uvicorn_error = logging.getLogger("uvicorn.error")
    uvicorn_error.disabled = True
    uvicorn_access = logging.getLogger("uvicorn.access")
    uvicorn_access.disabled = True
