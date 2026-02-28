import functools
import importlib.metadata
import platform
import traceback
from typing import Any, Dict, Optional

import structlog
import typer
from posthog import Posthog

from skyvern.config import settings

LOG = structlog.get_logger(__name__)

posthog = Posthog(
    "phc_bVT2ugnZhMHRWqMvSRHPdeTjaPxQqT3QSsI3r5FlQR5",
    host="https://app.posthog.com",
    disable_geoip=False,
    timeout=2,
)

DISTINCT_ID = "oss"


def get_oss_version() -> str:
    try:
        return importlib.metadata.version("skyvern")
    except Exception:
        return "unknown"


@functools.lru_cache(maxsize=1)
def analytics_metadata() -> Dict[str, Any]:
    # Cached: all fields are process-lifetime constants. Do not add dynamic fields here.
    return {
        "os": platform.system().lower(),
        "oss_version": get_oss_version(),
        "machine": platform.machine(),
        "platform": platform.platform(),
        "python_version": platform.python_version(),
        "environment": settings.ENV,
    }


def capture(
    event: str,
    data: dict[str, Any] | None = None,
) -> None:
    if not settings.SKYVERN_TELEMETRY:
        return

    try:
        distinct_id = settings.ANALYTICS_ID
        payload: dict[str, Any] = data or {}
        posthog.capture(distinct_id=distinct_id, event=event, properties=payload)
    except Exception:
        LOG.debug("analytics capture failed", event=event, exc_info=True)


def capture_setup_event(
    event_name: str,
    success: bool = True,
    error_type: Optional[str] = None,
    error_message: Optional[str] = None,
    extra_data: Optional[dict[str, Any]] = None,
) -> None:
    """Capture a setup-related analytics event.

    Args:
        event_name: The event name (will be prefixed with 'skyvern-oss-setup-')
        success: Whether the setup step succeeded
        error_type: Type/category of error (e.g., 'docker_not_running', 'port_conflict')
        error_message: Human-readable error message
        extra_data: Additional event properties
    """
    data: dict[str, Any] = {
        **analytics_metadata(),
        "success": success,
    }
    if error_type:
        data["error_type"] = error_type
    if error_message:
        data["error_message"] = error_message
    if extra_data:
        data.update(extra_data)

    capture(f"skyvern-oss-setup-{event_name}", data)


def capture_setup_error(
    event_name: str,
    error: Exception,
    error_type: Optional[str] = None,
    extra_data: Optional[dict[str, Any]] = None,
) -> None:
    """Capture a setup error with exception details.

    Args:
        event_name: The event name (will be prefixed with 'skyvern-oss-setup-')
        error: The exception that occurred
        error_type: Optional error type/category
        extra_data: Additional event properties
    """
    data: dict[str, Any] = {
        **analytics_metadata(),
        "success": False,
        "error_type": error_type or type(error).__name__,
        "error_message": str(error),
        "stack_trace": traceback.format_exc(),
    }
    if extra_data:
        data.update(extra_data)

    capture(f"skyvern-oss-setup-{event_name}", data)


# This is the main function that will be called by the typer CLI. This is separate from capture because typer
# doesn't support dict type input arguments.
def capture_simple(event: str) -> None:
    capture(event)


if __name__ == "__main__":
    typer.run(capture_simple)
