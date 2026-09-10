import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.unit.browser_extension.home_guard import isolate_browser_extension_home  # noqa: F401


@pytest.fixture
def short_broker_base_dir() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="be-", dir="/tmp") as base_dir:
        yield Path(base_dir)
