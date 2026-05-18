import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from skyvern.utils.env_paths import EnvIntent

_DEFAULT_CLI_LOG_LEVEL = "WARNING"

_QUIET_CLI_LOGGERS = ("skyvern", "httpx", "litellm", "playwright", "httpcore")
_RUNTIME_LOGGING_CONFIGURED = False
_SILENCED_CLI_LOGGERS = ("posthog",)


def raise_unless_missing_optional_dependency(exc: ImportError, expected_modules: set[str]) -> None:
    if isinstance(exc, ModuleNotFoundError) and exc.name in expected_modules:
        return
    raise exc


def _resolve_cli_log_level_name() -> str:
    """Honor explicit process env while keeping CLI defaults quiet."""
    return os.environ.get("LOG_LEVEL", _DEFAULT_CLI_LOG_LEVEL).upper()


def configure_cli_bootstrap_logging() -> None:
    """Clamp CLI process logging before importing the command tree."""
    log_level_name = _resolve_cli_log_level_name()
    log_level = logging.getLevelName(log_level_name)
    if not isinstance(log_level, int):
        log_level = logging.WARNING
    logging.getLogger().setLevel(log_level)
    for logger_name in _QUIET_CLI_LOGGERS:
        logging.getLogger(logger_name).setLevel(log_level)
    for logger_name in _SILENCED_CLI_LOGGERS:
        logger = logging.getLogger(logger_name)
        logger.setLevel(logging.CRITICAL + 1)
        logger.propagate = False


def configure_cli_runtime_logging() -> None:
    """Configure full Skyvern logging after CLI env intent has been selected."""
    global _RUNTIME_LOGGING_CONFIGURED

    if _RUNTIME_LOGGING_CONFIGURED:
        return

    from skyvern.forge.sdk.forge_log import setup_logger  # noqa: PLC0415

    setup_logger()
    _RUNTIME_LOGGING_CONFIGURED = True


def prepare_cli_runtime(intent: "EnvIntent | str") -> Path:
    """Load intent-scoped env files, then configure logging that imports settings."""
    from skyvern.utils.env_paths import load_backend_env_files  # noqa: PLC0415

    env_path = load_backend_env_files(intent=intent)
    configure_cli_runtime_logging()
    return env_path
