import functools
import importlib.metadata
import platform
from typing import Any, Dict

import structlog
import typer
from posthog import Posthog

from skyvern.config import settings

LOG = structlog.get_logger(__name__)

posthog = Posthog(
    "phc_bVT2ugnZhMHRWqMvSRHPdeTjaPxQqT3QSsI3r5FlQR5",
    host="https://app.posthog.com",
    disable_geoip=False,
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


# This is the main function that will be called by the typer CLI. This is separate from capture because typer
# doesn't support dict type input arguments.
def capture_simple(event: str) -> None:
    capture(event)


if __name__ == "__main__":
    typer.run(capture_simple)
