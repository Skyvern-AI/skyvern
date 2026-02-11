from __future__ import annotations

import os
from contextvars import ContextVar

from testcharmvision.client import TestcharmvisionEnvironment
from testcharmvision.config import settings
from testcharmvision.library.testcharmvision import Testcharmvision

_testcharmvision_instance: ContextVar[Testcharmvision | None] = ContextVar("testcharmvision_instance", default=None)


def get_testcharmvision() -> Testcharmvision:
    """Get or create a Testcharmvision client instance."""
    instance = _testcharmvision_instance.get()
    if instance is not None:
        return instance

    api_key = settings.TESTCHARMVISION_API_KEY or os.environ.get("TESTCHARMVISION_API_KEY")
    base_url = settings.TESTCHARMVISION_BASE_URL or os.environ.get("TESTCHARMVISION_BASE_URL")

    if api_key:
        instance = Testcharmvision(
            api_key=api_key,
            environment=TestcharmvisionEnvironment.CLOUD,
            base_url=base_url,
        )
    else:
        instance = Testcharmvision.local()

    _testcharmvision_instance.set(instance)
    return instance
