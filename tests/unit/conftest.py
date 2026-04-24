"""Shared pytest fixtures and setup for unit tests."""

# -- begin speed up unit tests
from unittest.mock import AsyncMock, MagicMock

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

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


# -- shared OTEL span capture for tests that assert on span attributes --
#
# OTEL's global TracerProvider can only be set once per process. We install a
# single TracerProvider + InMemorySpanExporter at session start; tests that
# need span capture depend on the `span_exporter` fixture and get a cleared
# exporter for each test.

_SHARED_SPAN_EXPORTER: InMemorySpanExporter | None = None


def _install_span_exporter() -> InMemorySpanExporter:
    global _SHARED_SPAN_EXPORTER
    if _SHARED_SPAN_EXPORTER is None:
        exporter = InMemorySpanExporter()
        provider = otel_trace.get_tracer_provider()
        if isinstance(provider, TracerProvider):
            provider.add_span_processor(SimpleSpanProcessor(exporter))
        else:
            provider = TracerProvider()
            provider.add_span_processor(SimpleSpanProcessor(exporter))
            otel_trace.set_tracer_provider(provider)
        _SHARED_SPAN_EXPORTER = exporter
    return _SHARED_SPAN_EXPORTER


@pytest.fixture
def span_exporter() -> InMemorySpanExporter:
    exporter = _install_span_exporter()
    exporter.clear()
    yield exporter
    exporter.clear()
