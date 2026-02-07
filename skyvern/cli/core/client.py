from __future__ import annotations

import os
from contextvars import ContextVar

from skyvern.client import SkyvernEnvironment
from skyvern.config import settings
from skyvern.library.skyvern import Skyvern

_skyvern_instance: ContextVar[Skyvern | None] = ContextVar("skyvern_instance", default=None)


def get_skyvern() -> Skyvern:
    """Get or create a Skyvern client instance."""
    instance = _skyvern_instance.get()
    if instance is not None:
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

    _skyvern_instance.set(instance)
    return instance
