import logging
from collections.abc import Set

_DEFAULT_CLI_LOG_LEVEL = "WARNING"

_QUIET_CLI_LOGGERS = ("skyvern", "httpx", "litellm", "playwright", "httpcore")


def _resolve_cli_log_level_name(settings: object) -> str:
    """Honor explicit settings while keeping CLI defaults quiet."""

    fields_set: Set[str] = getattr(settings, "model_fields_set", set())
    configured_level = str(getattr(settings, "LOG_LEVEL", _DEFAULT_CLI_LOG_LEVEL)).upper()
    if "LOG_LEVEL" in fields_set:
        return configured_level
    return _DEFAULT_CLI_LOG_LEVEL


def configure_cli_bootstrap_logging() -> None:
    """Clamp CLI process logging before importing the command tree."""
    from skyvern.config import settings  # noqa: PLC0415
    from skyvern.forge.sdk.forge_log import setup_logger  # noqa: PLC0415

    log_level_name = _resolve_cli_log_level_name(settings)
    settings.LOG_LEVEL = log_level_name
    setup_logger()
    log_level = logging.getLevelName(log_level_name)
    if not isinstance(log_level, int):
        log_level = logging.WARNING
    logging.getLogger().setLevel(log_level)
    for logger_name in _QUIET_CLI_LOGGERS:
        logging.getLogger(logger_name).setLevel(log_level)
