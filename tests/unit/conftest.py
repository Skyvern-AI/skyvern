"""Shared pytest fixtures and setup for unit tests."""

# -- begin speed up unit tests
import itertools
import logging
import shutil
from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
import structlog
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.db.models import Base
from tests.unit.force_stub_app import start_forge_stub_app

# Wire structlog through stdlib so caplog can capture log records in tests.
structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    logger_factory=structlog.stdlib.LoggerFactory(),
)

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


# -- shared copilot agent-template rendering helper --

_AGENT_TEMPLATE_DEFAULTS = dict(
    workflow_knowledge_base="test kb",
    current_datetime="2026-01-01T00:00:00Z",
    tool_usage_guide="",
    security_rules="",
)


def render_agent_prompt(**overrides: str) -> str:
    """Render the workflow-copilot-agent template with test defaults; overrides replace named params."""
    return prompt_engine.load_prompt("workflow-copilot-agent", **{**_AGENT_TEMPLATE_DEFAULTS, **overrides})


def make_copilot_context(workflow_yaml: str = "") -> CopilotContext:
    return CopilotContext(
        organization_id="o",
        workflow_id="w",
        workflow_permanent_id="wp",
        workflow_yaml=workflow_yaml,
        browser_session_id=None,
        stream=SimpleNamespace(),  # type: ignore[arg-type]
    )


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


# -- shared in-memory SQLite engine for repository/route unit tests --
#
# ``Base.metadata.create_all`` issues DDL for every mapped table (~50) on every
# call, so re-running it per test dominates the runtime of the repository suites.
# We build the schema once per session into a template SQLite file and clone that
# file per test — a byte copy is orders of magnitude cheaper than re-emitting the
# DDL, and each test still gets its own isolated database.


@pytest.fixture(scope="session")
def sqlite_schema_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    template_path = tmp_path_factory.mktemp("sqlite_schema") / "schema.db"
    engine = create_engine(f"sqlite:///{template_path}")
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()
    return template_path


@pytest_asyncio.fixture
async def sqlite_engine_factory(
    sqlite_schema_template: Path, tmp_path: Path
) -> AsyncGenerator[Callable[[], AsyncEngine]]:
    engines: list[AsyncEngine] = []
    counter = itertools.count()

    def _make() -> AsyncEngine:
        db_path = tmp_path / f"db_{next(counter)}.db"
        shutil.copyfile(sqlite_schema_template, db_path)
        engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        engines.append(engine)
        return engine

    yield _make

    for engine in engines:
        await engine.dispose()


@pytest_asyncio.fixture
async def sqlite_engine(sqlite_engine_factory: Callable[[], AsyncEngine]) -> AsyncEngine:
    return sqlite_engine_factory()
