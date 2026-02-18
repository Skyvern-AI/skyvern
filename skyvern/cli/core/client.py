from __future__ import annotations

import os
from contextvars import ContextVar

import structlog

from skyvern.client import SkyvernEnvironment
from skyvern.config import settings
from skyvern.library.skyvern import Skyvern

_skyvern_instance: ContextVar[Skyvern | None] = ContextVar("skyvern_instance", default=None)
_global_skyvern_instance: Skyvern | None = None
LOG = structlog.get_logger(__name__)


def get_skyvern() -> Skyvern:
    """Get or create a Skyvern client instance."""
    global _global_skyvern_instance

    instance = _skyvern_instance.get()
    if instance is None:
        instance = _global_skyvern_instance
    if instance is not None:
        _skyvern_instance.set(instance)
        return instance

    api_key = settings.SKYVERN_API_KEY or os.environ.get("SKYVERN_API_KEY")
    base_url = settings.SKYVERN_BASE_URL or os.environ.get("SKYVERN_BASE_URL")

    if api_key:
        instance = Skyvern(
            api_key=api_key,
            environment=SkyvernEnvironment.CLOUD,
            base_url=base_url,
        )
    else:
        instance = Skyvern.local()

    _global_skyvern_instance = instance
    _skyvern_instance.set(instance)
    return instance


async def close_skyvern() -> None:
    """Close active Skyvern client(s) and release Playwright resources."""
    global _global_skyvern_instance

    instances: list[Skyvern] = []
    seen: set[int] = set()
    for candidate in (_skyvern_instance.get(), _global_skyvern_instance):
        if candidate is None or id(candidate) in seen:
            continue
        seen.add(id(candidate))
        instances.append(candidate)

    for instance in instances:
        try:
            await instance.aclose()
        except Exception:
            LOG.warning("Failed to close Skyvern client", exc_info=True)

    _skyvern_instance.set(None)
    _global_skyvern_instance = None
