# -- begin speed up unit tests
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.unit.force_stub_app import start_forge_stub_app

# NOTE(jdo): uncomment below to run tests faster, if you're targetting smth
# that does not need the full app context

# import sys
# from unittest.mock import MagicMock

# mock_modules = [
#     "skyvern.forge.app",
#     "skyvern.library",
#     "skyvern.core.script_generations.skyvern_page",
#     "skyvern.core.script_generations.run_initializer",
#     "skyvern.core.script_generations.workflow_wrappers",
#     "skyvern.services.script_service",
# ]

# for module in mock_modules:
#     sys.modules[module] = MagicMock()

# -- end speed up unit tests


@pytest.fixture(scope="module", autouse=True)
def setup_forge_stub_app():
    start_forge_stub_app()
    yield


# -- shared helpers for repository unit tests --


class MockAsyncSessionCtx:
    """Async context manager wrapping a mock SQLAlchemy session for repository tests."""

    def __init__(self, session: AsyncMock):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *args):
        pass


def make_mock_session(mock_model: MagicMock) -> AsyncMock:
    """Create a mock SQLAlchemy session that returns mock_model from scalars().first()."""
    scalars_result = MagicMock()
    scalars_result.first.return_value = mock_model

    mock_session = AsyncMock()
    mock_session.scalars.return_value = scalars_result
    mock_session.commit = AsyncMock()
    mock_session.refresh = AsyncMock()

    return mock_session
