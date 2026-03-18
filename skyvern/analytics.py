import functools
import importlib.metadata
import platform
import traceback
from typing import Any, Dict, Optional

import structlog
import typer
from posthog import Posthog

from skyvern._version import __version__ as _build_version
from skyvern.config import settings

LOG = structlog.get_logger(__name__)


def _build_posthog_client(api_key: str, host: str) -> Posthog:
    return Posthog(api_key, host=host, disable_geoip=False, timeout=2)


posthog = _build_posthog_client(
    settings.POSTHOG_PROJECT_API_KEY,
    settings.POSTHOG_PROJECT_HOST,
)
_custom_posthog_clients: dict[tuple[str, str], Posthog] = {}

DISTINCT_ID = "oss"


def get_oss_version() -> str:
    # CI builds stamp skyvern/_version.py with the git SHA; prefer that.
    if _build_version and _build_version != "development":
        return _build_version
    # Fallback for pip-installed environments (e.g. OSS users)
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


def dynamic_analytics_metadata() -> Dict[str, Any]:
    metadata: dict[str, Any] = {}
    if settings.ANALYTICS_TEST_ID:
        metadata["analytics_test_id"] = settings.ANALYTICS_TEST_ID
    return metadata


def reconfigure_posthog_client(
    api_key: str | None = None,
    host: str | None = None,
) -> None:
    global posthog
    posthog = _build_posthog_client(
        api_key or settings.POSTHOG_PROJECT_API_KEY,
        host or settings.POSTHOG_PROJECT_HOST,
    )


def _resolve_posthog_client(
    api_key: str | None = None,
    host: str | None = None,
) -> Posthog:
    if api_key is None and host is None:
        return posthog

    resolved_api_key = api_key or settings.POSTHOG_PROJECT_API_KEY
    resolved_host = host or settings.POSTHOG_PROJECT_HOST
    cache_key = (resolved_api_key, resolved_host)
    client = _custom_posthog_clients.get(cache_key)
    if client is None:
        client = _build_posthog_client(resolved_api_key, resolved_host)
        _custom_posthog_clients[cache_key] = client
    return client


def flush(
    api_key: str | None = None,
    host: str | None = None,
) -> None:
    _resolve_posthog_client(api_key=api_key, host=host).flush()


def capture(
    event: str,
    data: dict[str, Any] | None = None,
    distinct_id: str | None = None,
    api_key: str | None = None,
    host: str | None = None,
) -> None:
    if not settings.SKYVERN_TELEMETRY:
        return

    try:
        resolved_distinct_id = distinct_id or settings.ANALYTICS_ID
        payload: dict[str, Any] = {**dynamic_analytics_metadata(), **(data or {})}
        client = _resolve_posthog_client(api_key=api_key, host=host)
        client.capture(distinct_id=resolved_distinct_id, event=event, properties=payload)
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
