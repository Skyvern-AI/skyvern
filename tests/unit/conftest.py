"""Shared pytest fixtures and setup for unit tests."""

# -- begin speed up unit tests
import asyncio
import contextlib
import hashlib
import itertools
import logging
import os
import shutil
import sys
import threading
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Coroutine, Iterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TypeVar
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
import structlog
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from playwright.async_api import Download
from playwright.async_api import Error as PlaywrightError
from sqlalchemy import Engine, create_engine, func, select
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

import skyvern._cli_bootstrap as cli_bootstrap
from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.agent_functions import AgentFunction
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.api import files
from skyvern.forge.sdk.artifact.models import Artifact
from skyvern.forge.sdk.artifact.storage.base import BaseStorage
from skyvern.forge.sdk.copilot.context import (
    CopilotContext,
    model_written_context_fields,
    tool_recorded_context_fields,
)
from skyvern.forge.sdk.credential_site_policy import LoginSite
from skyvern.forge.sdk.db.agent_db import AgentDB
from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
from skyvern.forge.sdk.db.models import Base, CredentialModel, WorkflowModel
from skyvern.forge.sdk.db.utils import _custom_json_serializer
from skyvern.forge.sdk.executor.async_executor import AsyncExecutor
from skyvern.forge.sdk.executor.factory import AsyncExecutorFactory
from skyvern.forge.sdk.schemas.credentials import BitwardenItemOverview, CredentialType
from skyvern.forge.sdk.schemas.files import FileInfo
from skyvern.forge.sdk.schemas.organizations import BitwardenCredential, BitwardenOrganizationAuthToken, Organization
from skyvern.forge.sdk.services.bitwarden import BitwardenConstants, BitwardenService
from skyvern.forge.sdk.settings_manager import SettingsManager
from skyvern.forge.sdk.workflow import web_search, web_search_client
from skyvern.forge.sdk.workflow.context_manager import WorkflowContextManager
from skyvern.forge.sdk.workflow.models.block import BlockTypeVar, TaskBlock
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter, WorkflowParameterType
from skyvern.forge.sdk.workflow.models.workflow import WorkflowDefinition, WorkflowRunStatus
from skyvern.forge.sdk.workflow.service import WorkflowService
from skyvern.services import workflow_run_group_service as group_service
from skyvern.webeye.utils import page as page_module
from skyvern.webeye.utils.page import ScreenshotMode
from tests.unit._fingerprint_expectations import FINGERPRINT_TEST_SECRET_KEY
from tests.unit.dns_fixtures import no_env_proxy, public_dns, unresolvable_dns  # noqa: F401
from tests.unit.force_stub_app import start_forge_stub_app
from tests.unit.google.conftest import mock_sheets_transport  # noqa: F401
from tests.unit.litellm_model_registry import registered_gpt56_litellm_models

# Four distinct ways to leave the legacy downloads root; each defeats a different weak check.
LEGACY_DOWNLOAD_ESCAPE_CASES = ("parent_traversal", "encoded_dot_dot", "sibling_prefix", "symlink_escape")

LOOPBACK_POSTGRES_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
# The schema fixture only adds and drops a schema, so it also accepts the compose service name.
LOCAL_POSTGRES_HOSTS = LOOPBACK_POSTGRES_HOSTS | {"postgres"}
# Any other query key (host, hostaddr, service, dbname, port, user, options, ...) can change which server or database
# libpq reaches, so unknown keys are rejected instead of listing the bad ones.
POSTGRES_NON_ROUTING_QUERY_KEYS = frozenset({"connect_timeout", "application_name", "sslmode"})
# These env vars redirect a connection even when the URL names its host; PGPORT does so only when the URL has no port.
POSTGRES_HOST_OVERRIDE_ENV_VARS = ("PGHOSTADDR", "PGSERVICE")


def skip_unless_local_postgres(url: URL, hosts: frozenset[str] = LOCAL_POSTGRES_HOSTS) -> None:
    if url.get_backend_name() != "postgresql" or url.host not in hosts:
        pytest.skip(f"needs a PostgreSQL on one of: {', '.join(sorted(hosts))}")
    overrides = sorted(set(url.query) - POSTGRES_NON_ROUTING_QUERY_KEYS)
    overrides += [name for name in POSTGRES_HOST_OVERRIDE_ENV_VARS if os.environ.get(name)]
    if url.port is None and os.environ.get("PGPORT"):
        overrides.append("PGPORT")
    if overrides:
        pytest.skip(f"{', '.join(overrides)} could redirect the connection away from the intended local PostgreSQL")


def fake_download_registration_app(
    register: Callable[..., Awaitable[None]], rows: list[Artifact], storage: BaseStorage | None = None
) -> SimpleNamespace:
    return SimpleNamespace(
        STORAGE=storage,
        ARTIFACT_MANAGER=SimpleNamespace(create_download_artifact=register),
        DATABASE=SimpleNamespace(
            artifacts=SimpleNamespace(list_artifacts_for_run_by_type=AsyncMock(side_effect=lambda **kwargs: list(rows)))
        ),
    )


class FakeWorkflowRunAttemptsRepository:
    """Small in-memory repository for tests that need durable attempt resolution."""

    def __init__(self, attempts: list[Any] | None = None) -> None:
        self.attempts = list(attempts or [])
        self.requested_workflow_run_ids: list[str] = []

    async def get_attempts(self, workflow_run_id: str) -> list[Any]:
        self.requested_workflow_run_ids.append(workflow_run_id)
        return list(self.attempts)

    async def refresh_attempt_finished_at(self, workflow_run_id: str, attempt_number: int, *, finished_at: Any) -> Any:
        for attempt in self.attempts:
            if attempt.workflow_run_id == workflow_run_id and attempt.attempt_number == attempt_number:
                if getattr(attempt, "retry_decision", None) is not None:
                    attempt.finished_at = finished_at
                return attempt
        return None


@pytest.fixture
def legacy_download_uris(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """file:// URIs into a synthetic legacy repo root: one canonical file plus every escape class.

    Points the module's ``REPO_ROOT_DIR`` at the temporary root, so anything reaching the legacy
    file:// branch resolves against this lab rather than the real repository.
    """
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    (downloads / "STORMBREAKER-safe.txt").write_text("STORMBREAKER-safe-body")
    (tmp_path / "downloads-evil").mkdir()
    (tmp_path / "downloads-evil" / "STORMBREAKER-secret.txt").write_text("STORMBREAKER-sibling-secret")
    (tmp_path / "outside").mkdir()
    outside_secret = tmp_path / "outside" / "STORMBREAKER-secret.txt"
    outside_secret.write_text("STORMBREAKER-outside-secret")
    (downloads / "STORMBREAKER-link").symlink_to(outside_secret)

    monkeypatch.setattr(files, "REPO_ROOT_DIR", tmp_path)
    return {
        "canonical": (downloads / "STORMBREAKER-safe.txt").as_uri(),
        "parent_traversal": (downloads / ".." / "outside" / "STORMBREAKER-secret.txt").as_uri(),
        # Percent-encoded, so a check running before URL decoding cannot be what blocks it.
        "encoded_dot_dot": f"file://{downloads}/%2E%2E/outside/STORMBREAKER-secret.txt",
        "sibling_prefix": (tmp_path / "downloads-evil" / "STORMBREAKER-secret.txt").as_uri(),
        "symlink_escape": (downloads / "STORMBREAKER-link").as_uri(),
    }


@pytest.fixture
def fingerprint_secret_key(monkeypatch: pytest.MonkeyPatch) -> str:
    """Pin ``SECRET_KEY`` so ``diagnostic_fingerprint`` produces stable, keyed output in tests.

    Patches the shared ``settings`` singleton, so it is seen wherever the helper reads it.
    """
    from skyvern.config import settings

    monkeypatch.setattr(settings, "SECRET_KEY", FINGERPRINT_TEST_SECRET_KEY)
    return FINGERPRINT_TEST_SECRET_KEY


@pytest.fixture
def workflow_context_manager_factory() -> Callable[..., WorkflowContextManager]:
    def _make(
        *,
        workflow_run_id: str = "wr_mask_secrets",
        mask_secrets: bool = True,
        secrets: dict[str, str] | None = None,
        runtime_otp_values: set[str] | None = None,
        attempt_number: int = 1,
    ) -> WorkflowContextManager:
        manager = WorkflowContextManager()
        manager.workflow_run_contexts[workflow_run_id] = SimpleNamespace(
            mask_secrets=mask_secrets,
            secrets=dict(secrets or {}),
            runtime_otp_values=set(runtime_otp_values or set()),
            attempt_number=attempt_number,
        )
        return manager

    return _make


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


def cancel_leftover_tasks(loop: asyncio.AbstractEventLoop) -> None:
    # A task left suspended in a SQLAlchemy session on a closed loop is later finalized by a garbage collection
    # inside another test's session, which fails that test with GeneratorExit and stops all later collections.
    cancelled: set[asyncio.Task[Any]] = set()
    for _ in range(3):
        leftover = {task for task in asyncio.all_tasks(loop) if not task.done()}
        if not leftover:
            break
        for task in leftover:
            task.cancel()
        cancelled |= leftover
        # Bounded and repeated, as asyncio.run is not: a task that swallows one cancel must not hang teardown.
        loop.run_until_complete(asyncio.wait(leftover, timeout=2))
    for task in cancelled:
        # Reading the exception also marks it retrieved, so the loop handler reports it once.
        if task.done() and not task.cancelled() and task.exception() is not None:
            loop.call_exception_handler(
                {
                    "message": "unhandled exception during test loop teardown",
                    "exception": task.exception(),
                    "task": task,
                }
            )
    for task in asyncio.all_tasks(loop):
        loop.call_exception_handler(
            {"message": "task still pending after cancellation at test loop teardown", "task": task}
        )


@pytest.fixture
def event_loop() -> Iterator[asyncio.AbstractEventLoop]:
    # pytest-asyncio's default loop closes without cancelling the tasks a test left running.
    loop = asyncio.new_event_loop()
    yield loop
    if not loop.is_closed():
        cancel_leftover_tasks(loop)
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()


@pytest.fixture(autouse=True)
def reset_collapse_xp_assignment_memo():
    # The collapse umbrella memo is process-global by design; without clearing it,
    # an assignment memoized by one test leaks into any later test reusing the same task id.
    def _clear() -> None:
        handler_module = sys.modules.get("skyvern.webeye.actions.handler")
        if handler_module is not None:
            handler_module._COLLAPSE_XP_ASSIGNMENT_MEMO.clear()

    _clear()
    yield
    _clear()


@pytest.fixture(autouse=True)
def reset_copilot_driver_ledgers() -> Iterator[None]:
    # A holder count leaked by one test makes a later test's turn-exit release silently skip its evict.
    def _clear() -> None:
        runtime = sys.modules.get("skyvern.forge.sdk.copilot.runtime")
        if runtime is not None:
            runtime._ATTACHED_TURNS_PER_SESSION.clear()
            runtime._DRIVER_RELEASES_IN_FLIGHT.clear()
            runtime._DRIVER_RELEASE_EPOCHS.clear()
            runtime._SCRUB_VALUES_CLEARED_ON_RELEASE.clear()

    _clear()
    yield
    _clear()


@pytest.fixture(autouse=True)
def restore_interpreter_traceback_hooks() -> Iterator[None]:
    """setup_logger() replaces the three interpreter hooks process-wide.

    Left installed they outlive the test that configured logging and shadow pytest's own
    unraisable/thread-exception plugins, which install their hooks per test.
    """
    hooks = (sys.excepthook, threading.excepthook, sys.unraisablehook)
    yield
    sys.excepthook, threading.excepthook, sys.unraisablehook = hooks


@pytest.fixture(autouse=True)
def reset_cli_runtime_entry() -> Iterator[None]:
    """A CliRunner invocation marks the whole process as CLI-entered and loads a backend env file.

    A CLI command also labels the process's API client as the CLI, which must not leak into a later MCP test.

    Both outlive the test, and the pair trips the CLI-only guard that refuses an API key
    against the default production URL in any later test that builds a cloud client. The env
    load also records SKYVERN_ENV_INTENT unconditionally, which config reads for env precedence.
    """
    entered = cli_bootstrap._CLI_RUNTIME_PREPARED
    command_running = cli_bootstrap._CLI_COMMAND_RUNNING
    loaded = {name: os.environ.get(name) for name in ("SKYVERN_API_KEY", "SKYVERN_BASE_URL", "SKYVERN_ENV_INTENT")}
    yield
    cli_bootstrap._CLI_RUNTIME_PREPARED = entered
    cli_bootstrap._CLI_COMMAND_RUNNING = command_running
    for name, value in loaded.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


@pytest.fixture(autouse=True)
def reset_mcp_stateless_http_mode():
    """Keep MCP transport mode from leaking between independently collected test files."""
    from skyvern.cli.core import session_manager

    session_manager.set_stateless_http_mode(False)
    yield
    session_manager.set_stateless_http_mode(False)


# -- shared copilot agent-template rendering helper --

_AGENT_TEMPLATE_DEFAULTS = dict(
    workflow_knowledge_base="test kb",
    current_datetime="2026-01-01T00:00:00Z",
    tool_usage_guide="",
    security_rules="",
    model_written_context_fields=model_written_context_fields(),
    tool_recorded_context_fields=tool_recorded_context_fields(),
)


def render_agent_prompt(**overrides: str) -> str:
    """Render the workflow-copilot-agent template with test defaults; overrides replace named params."""
    return prompt_engine.load_prompt("workflow-copilot-agent", **{**_AGENT_TEMPLATE_DEFAULTS, **overrides})


def make_block_output_parameter(key: str = "block_output", workflow_id: str = "workflow-id") -> OutputParameter:
    now = datetime.now(UTC)
    return OutputParameter(
        output_parameter_id=f"{key}_id", key=key, workflow_id=workflow_id, created_at=now, modified_at=now
    )


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


@pytest.fixture(scope="module")
def gpt56_litellm_models() -> Iterator[None]:
    with registered_gpt56_litellm_models():
        yield


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
        engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", json_serializer=_custom_json_serializer)
        engines.append(engine)
        return engine

    yield _make

    for engine in engines:
        await engine.dispose()


@pytest_asyncio.fixture
async def sqlite_engine(sqlite_engine_factory: Callable[[], AsyncEngine]) -> AsyncEngine:
    return sqlite_engine_factory()


@pytest.fixture
def postgres_scratch_database() -> Iterator[Engine]:
    url = make_url(str(settings.DATABASE_STRING))
    # Loopback only: a generic name like postgres could resolve to a shared server, and this drops a database.
    skip_unless_local_postgres(url, LOOPBACK_POSTGRES_HOSTS)
    url = url.set(drivername="postgresql+psycopg")
    # A database, not a schema: CREATE INDEX CONCURRENTLY waits for every older snapshot in its database, so a
    # transaction another test leaves open in the shared one would stall the build until statement_timeout.
    database = f"test_scratch_{uuid.uuid4().hex}"
    admin = create_engine(url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            if not connection.exec_driver_sql(
                "SELECT rolcreatedb OR rolsuper FROM pg_roles WHERE rolname = current_user"
            ).scalar_one():
                pytest.skip("needs a PostgreSQL role that can create databases")
            connection.exec_driver_sql(f'CREATE DATABASE "{database}"')
        engine = create_engine(url.set(database=database))
        try:
            yield engine
        finally:
            engine.dispose()
            with admin.connect() as connection:
                connection.exec_driver_sql(f'DROP DATABASE "{database}" WITH (FORCE)')
    finally:
        admin.dispose()


def make_input_element_mock(*, element_id: str = "AADC", attrs: dict[str, object] | None = None) -> MagicMock:
    # SkyvernElement double for handle_input_text_action tests. attrs=None makes every get_attr return
    # None (plain search-bar case); pass a dict to drive specific attrs (e.g. a combobox's role /
    # aria-autocomplete / aria-invalid).
    el = MagicMock()
    el.get_id.return_value = element_id
    el.get_tag_name.return_value = "input"
    el.get_frame.return_value = MagicMock()
    locator = MagicMock()
    locator.focus = AsyncMock()
    el.get_locator.return_value = locator
    el.is_disabled = AsyncMock(return_value=False)
    el.get_selectable = AsyncMock(return_value=False)
    el.has_hidden_attr = AsyncMock(return_value=False)
    el.is_readonly = AsyncMock(return_value=False)
    el.has_attr = AsyncMock(return_value=False)
    el.is_spinbtn_input = AsyncMock(return_value=False)
    el.is_editable = AsyncMock(return_value=True)
    el.supports_text_input = AsyncMock(return_value=True)
    el.is_visible = AsyncMock(return_value=True)
    el.is_raw_input = AsyncMock(return_value=False)
    el.is_auto_completion_input = AsyncMock(return_value=False)
    el.find_blocking_element = AsyncMock(return_value=(None, False))
    el.get_element_handler = AsyncMock(return_value=MagicMock())
    el.input_sequentially = AsyncMock()
    el.input_clear = AsyncMock()
    el.input_fill = AsyncMock()
    el.is_content_editable = AsyncMock(return_value=False)
    el.refresh_locator_if_stale = AsyncMock()
    el.apply_secret_visual_mask = AsyncMock()
    el.scroll_into_view = AsyncMock()
    el.press_key = AsyncMock()
    el.blur = AsyncMock()
    if attrs is None:
        el.get_attr = AsyncMock(return_value=None)
    else:

        def _get_attr(name: str, *args: object, **kwargs: object) -> object:
            return attrs.get(name)

        el.get_attr = AsyncMock(side_effect=_get_attr)
    return el


def make_claimed_download_mock(
    *,
    path: Path | str | None,
    suggested_filename: str,
    path_error: BaseException | None = None,
    failure: str | None = None,
    failure_error: BaseException | None = None,
    context: object | None = None,
) -> Download:
    """A Playwright ``Download`` double for the value a ``page.expect_download`` claim resolves to.
    ``spec`` is the real class so the attach guard's ``isinstance`` check sees what it does live."""
    download = MagicMock(spec=Download)
    if path_error is not None:
        download.path.side_effect = path_error
    else:
        download.path.return_value = None if path is None else str(path)
    if failure_error is not None:
        download.failure.side_effect = failure_error
    else:
        download.failure.return_value = failure
    download.suggested_filename = suggested_filename
    download.page = SimpleNamespace(context=context)
    return download


SESSION_DOWNLOAD_BYTES = b"session-delivered certificate"


def registered_download_row(
    filename: str = "certificate.pdf",
    content: bytes = SESSION_DOWNLOAD_BYTES,
    artifact_id: str | None = "a_session",
    checksum: str | None = None,
    file_size: int | None = None,
) -> FileInfo:
    """A DOWNLOAD row as the run's registration lists it for a file a remote browser session delivered."""
    return FileInfo(
        url=f"https://storage.test/{filename}",
        filename=filename,
        checksum=hashlib.sha256(content).hexdigest() if checksum is None else checksum,
        file_size=len(content) if file_size is None else file_size,
        artifact_id=artifact_id,
    )


@dataclass
class DownloadDestinationHarness:
    """A real HTTP server plus a stubbed resolver, for exercising download destination checks.

    Both host names answer on the same loopback server. ``public_host`` is allow-listed so it
    passes validation; ``internal_host`` is not, and resolves to a loopback address, so the
    validator must refuse it. Redirects are served for real, so a test never has to model how a
    given HTTP client follows them.
    """

    public_base: str
    internal_base: str
    other_base: str
    requested_paths: list[str]
    requested_hosts: list[str]
    cookies_by_path: dict[str, str]

    PUBLIC_BODY = b"%PDF-1.4 attachment payload"
    INTERNAL_BODY = b"INTERNAL-ONLY PAYLOAD"

    def reached_internal(self) -> bool:
        return any(host.startswith("internal-host.test") for host in self.requested_hosts)


@pytest.fixture
def download_destinations(monkeypatch: pytest.MonkeyPatch) -> Iterator[DownloadDestinationHarness]:
    import socket as socket_module
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from skyvern.config import settings

    public_host, internal_host, other_host = "public-host.test", "internal-host.test", "other-host.test"
    requested_paths: list[str] = []
    requested_hosts: list[str] = []
    cookies_by_path: dict[str, str] = {}
    holder: dict[str, str] = {}

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requested_paths.append(self.path)
            requested_hosts.append(self.headers.get("Host", ""))
            cookies_by_path[self.headers.get("Host", "").split(":")[0]] = self.headers.get("Cookie", "")
            if self.path in ("/redirect-to-internal", "/redirect-to-other"):
                target = holder["internal"] if self.path == "/redirect-to-internal" else holder["other"]
                self.send_response(302)
                self.send_header("Location", f"{target}/attachment")
                self.end_headers()
                return
            if self.path == "/notfound":
                body = b'{"error": "not found"}'
                self.send_response(404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            body = (
                DownloadDestinationHarness.INTERNAL_BODY
                if self.path == "/internal"
                else DownloadDestinationHarness.PUBLIC_BODY
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    holder["internal"] = f"http://{internal_host}:{port}"
    holder["other"] = f"http://{other_host}:{port}"

    real_getaddrinfo = socket_module.getaddrinfo
    mapped = {public_host, internal_host, other_host}

    def fake_getaddrinfo(host: str, port_arg: object = None, *args: object, **kwargs: object) -> list:
        if host in mapped:
            return [(socket_module.AF_INET, socket_module.SOCK_STREAM, 6, "", ("127.0.0.1", port_arg or 0))]
        return real_getaddrinfo(host, port_arg, *args, **kwargs)

    monkeypatch.setattr(socket_module, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(settings, "ALLOWED_HOSTS", [*settings.ALLOWED_HOSTS, public_host, other_host])

    try:
        yield DownloadDestinationHarness(
            public_base=f"http://{public_host}:{port}",
            internal_base=holder["internal"],
            other_base=holder["other"],
            requested_paths=requested_paths,
            requested_hosts=requested_hosts,
            cookies_by_path=cookies_by_path,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def fake_api_request_context() -> Callable[[], object]:
    """Build a stand-in for Playwright's ``APIRequestContext``.

    Requests are issued for real over HTTP. Redirect handling mirrors the driver's measured
    behaviour: hops are followed unless the caller passes ``max_redirects=0``, in which case the
    3xx response is returned with its ``Location`` header intact.
    """
    import asyncio
    import urllib.error
    import urllib.request

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args: object, **kwargs: object) -> None:
            return None

    class _Response:
        def __init__(self, status: int, headers: dict[str, str], body: bytes, url: str) -> None:
            self.status = status
            self.headers = headers
            self.url = url
            self._body = body

        @property
        def ok(self) -> bool:
            return 200 <= self.status < 300

        async def body(self) -> bytes:
            return self._body

    class _FakeAPIRequestContext:
        def __init__(self) -> None:
            self.requested_urls: list[str] = []

        async def get(self, url: str, max_redirects: int | None = None, **kwargs: object) -> _Response:
            self.requested_urls.append(url)

            def _fetch() -> _Response:
                opener = (
                    urllib.request.build_opener(_NoRedirect) if max_redirects == 0 else urllib.request.build_opener()
                )
                try:
                    with opener.open(urllib.request.Request(url)) as response:
                        return _Response(response.status, dict(response.headers), response.read(), response.url)
                except urllib.error.HTTPError as error:
                    return _Response(error.code, dict(error.headers), error.read(), url)

            return await asyncio.to_thread(_fetch)

    def _build() -> object:
        return _FakeAPIRequestContext()

    return _build


def serpapi_page(*links: str, next_start: int | None = None) -> dict[str, Any]:
    page: dict[str, Any] = {
        "search_metadata": {"status": "Success"},
        "organic_results": [{"title": f"Title {link}", "link": link, "snippet": f"About {link}"} for link in links],
    }
    if next_start is not None:
        page["serpapi_pagination"] = {"next": f"https://serpapi.com/search.json?start={next_start}"}
    return page


SearchApiReply = tuple[int, object] | BaseException


class FakeSearchApi:
    """Stands in for `aiohttp_request` under the search client: answers each call with the next queued
    (status, body) reply or raises it, repeating the last reply once the queue runs out."""

    def __init__(self, *replies: SearchApiReply) -> None:
        self._replies = list(replies)
        self.urls: list[str] = []

    async def __call__(self, *, url: str, **_kwargs: object) -> tuple[int, dict[str, str], object]:
        self.urls.append(url)
        reply = self._replies.pop(0) if len(self._replies) > 1 else self._replies[0]
        if isinstance(reply, BaseException):
            raise reply
        status, body = reply
        return status, {}, body


def arm_search_api(
    monkeypatch: pytest.MonkeyPatch,
    *replies: SearchApiReply,
    serpapi_key: str | None = "serp-test-key",
    exa_key: str | None = None,
) -> FakeSearchApi:
    """Configures the search keys, answers the vendor calls from `replies`, and admits every result
    destination; a test that screens destinations patches `web_search.classify_url_async` after this."""

    async def allow(_url: str) -> str | None:
        return None

    api = FakeSearchApi(*replies)
    monkeypatch.setattr(web_search_client, "aiohttp_request", api)
    monkeypatch.setattr(web_search_client.settings, "SERPAPI_API_KEY", serpapi_key)
    monkeypatch.setattr(web_search_client.settings, "EXA_API_KEY", exa_key)
    monkeypatch.setattr(SettingsManager.get_settings(), "ENABLE_SEARCH_WEB", True)
    monkeypatch.setattr(web_search, "classify_url_async", allow)
    return api


class FakeSearchPage:
    """A tab the block's browser context opens for an `open_page` call. A URL ending in ``/refused``
    fails to load; the title is derived from the URL."""

    def __init__(self, context: "FakeSearchBrowserContext | None" = None) -> None:
        self.context = context
        self.url = "about:blank"
        self.closed = False
        self.requested_url: str | None = None

    async def goto(self, url: str, timeout: float | None = None, **_kwargs: object) -> SimpleNamespace:
        self.requested_url = url
        if url.endswith("/refused"):
            raise PlaywrightError("net::ERR_FAILED")
        self.url = url
        return SimpleNamespace(status=200)

    async def title(self) -> str:
        return f"title of {self.url}"

    async def content(self) -> str:
        return ""

    def is_closed(self) -> bool:
        return self.closed

    async def close(self, **_kwargs: object) -> None:
        self.closed = True


class FakeSearchBrowserContext:
    def __init__(self) -> None:
        self.opened: list[FakeSearchPage] = []

    @property
    def page(self) -> FakeSearchPage:
        return self.opened[0]

    @property
    def pages(self) -> list[FakeSearchPage]:
        return list(self.opened)

    async def new_page(self) -> FakeSearchPage:
        page = FakeSearchPage(context=self)
        self.opened.append(page)
        return page


class FakeCdpSession:
    def __init__(
        self,
        storage_reachable: bool = True,
        origins_refusing_clear: tuple[str, ...] = (),
        origins_failing_unexpectedly: tuple[str, ...] = (),
        refuses_clear: bool = False,
        refusal_error: type[BaseException] = PlaywrightError,
        storage_key: str | None = None,
    ) -> None:
        self.sent: list[tuple[str, dict | None]] = []
        self.detached = False
        self.storage_key = storage_key
        self.storage_reachable = storage_reachable
        self.origins_refusing_clear = origins_refusing_clear
        self.origins_failing_unexpectedly = origins_failing_unexpectedly
        self.refuses_clear = refuses_clear
        self.refusal_error = refusal_error

    async def send(self, method: str, params: dict | None = None) -> dict:
        self.sent.append((method, params))
        if method == "Runtime.evaluate":
            # Answers for whatever document this session is attached to, as the real one does.
            return {"result": {"value": "reachable" if self.storage_reachable else "unreachable"}}
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": "frame-of-this-session"}}}
        if method == "Storage.getStorageKeyForFrame":
            if self.storage_key is None:
                raise self.refusal_error(
                    "Protocol error (Storage.getStorageKeyForFrame): Frame corresponds to an opaque origin"
                )
            return {"storageKey": self.storage_key}
        if method == "DOMStorage.clear":
            origin = (params or {}).get("storageId", {}).get("securityOrigin")
            if origin in self.origins_failing_unexpectedly:
                raise ValueError("not a browser-driver error")
            if self.refuses_clear or origin in self.origins_refusing_clear:
                raise self.refusal_error("Protocol error (DOMStorage.clear): Frame not found for the given storage id")
        return {}

    async def detach(self) -> None:
        self.detached = True


class FakeClearingBrowserContext:
    """Browser context a `clear_browser_data` call clears: records the cookie wipe and every CDP session it hands out.

    `pages` is what the clear enumerates origins from, so a test that expects storage to be cleared has
    to put its page in it, the way a live context holds its open tabs.
    """

    def __init__(self, clear_cookies_error: Exception | None = None) -> None:
        self.clear_cookies_calls = 0
        self.clear_cookies_error = clear_cookies_error
        self.cdp_sessions: list[tuple[object, FakeCdpSession]] = []
        self.pages: list[object] = []
        # Documents whose session reports no reachable storage, the way a sandboxed frame's does.
        self.frames_without_storage: list[object] = []
        # Origins whose DOMStorage.clear fails, whichever session carries it.
        self.origins_refusing_clear: list[str] = []
        # Frames sharing their parent's renderer: Playwright refuses them a session of their own.
        self.frames_without_own_session: list[object] = []
        # Origins whose clear fails with something that is not a browser-driver error at all.
        self.origins_failing_unexpectedly: list[str] = []
        # Frames whose own clear fails, however the origin is spelled -- a sandboxed frame does this
        # while an ordinary frame at the same origin clears fine.
        self.frames_refusing_clear: list[object] = []
        # Raise this engine's refusal instead of the Playwright family's, as a raw-CDP run would.
        self.refusal_error: type[BaseException] = PlaywrightError
        # Storage key the browser reports for a tab whose URL names no origin, as it does for a
        # window opened on about:blank. A tab absent from this list has an opaque origin and none.
        self.inherited_storage_keys: list[tuple[object, str]] = []
        # (frame, page) pairs whose frame session is attached to the page's target, as raw-CDP attaches
        # a same-process frame, so protocol calls on it answer for the page's document.
        self.frames_attached_to_page_target: list[tuple[object, object]] = []

    async def clear_cookies(self) -> None:
        self.clear_cookies_calls += 1
        if self.clear_cookies_error is not None:
            raise self.clear_cookies_error

    async def _probe_storage(self, document: object) -> str:
        return "unreachable" if document in self.frames_without_storage else "reachable"

    async def new_cdp_session(self, page: object) -> FakeCdpSession:
        if not hasattr(page, "evaluate"):
            page.evaluate = lambda expression, document=page: self._probe_storage(document)  # type: ignore[attr-defined]
        if page in self.frames_without_own_session:
            raise PlaywrightError("This frame does not have a separate CDP session")
        answering = next((held for frame, held in self.frames_attached_to_page_target if frame is page), page)
        session = FakeCdpSession(
            storage_reachable=answering not in self.frames_without_storage,
            origins_refusing_clear=tuple(self.origins_refusing_clear),
            origins_failing_unexpectedly=tuple(self.origins_failing_unexpectedly),
            refuses_clear=page in self.frames_refusing_clear,
            refusal_error=self.refusal_error,
            storage_key=next((key for held, key in self.inherited_storage_keys if held is page), None),
        )
        self.cdp_sessions.append((page, session))
        return session


def read_unit_data_fixture(name: str) -> str:
    return (Path(__file__).parent / "data" / name).read_text()


class ScopeRecordingAgentFunction(AgentFunction):
    """Records the captcha-solver lifecycle scope's enter/exit and — when ``record_arms`` — the extension
    resolver, the completion-confirmation probe, and each solver arm, proving the ladder resolves and confirms
    inside the open scope. ``record_arms=False`` silences those; ``confirm`` overrides the anchor arm's
    completion verdict (``None`` hands the ladder's own default back, like the OSS base)."""

    def __init__(self, *, auto_solve: bool = False, record_arms: bool = True, confirm: bool | None = None) -> None:
        self.events: list[str] = []
        self._auto_solve = auto_solve
        self._record_arms = record_arms
        self._confirm = confirm

    def captcha_solver_lifecycle_scope(self, page: object) -> AbstractAsyncContextManager[None]:
        events = self.events

        @asynccontextmanager
        async def _scope() -> AsyncIterator[None]:
            events.append("enter")
            try:
                yield
            finally:
                events.append("exit")

        return _scope()

    def resolve_captcha_solver_extension_timeout(self, page: object, default_timeout: float) -> float:
        if self._record_arms:
            self.events.append("resolve")
        return default_timeout

    async def is_captcha_solver_completion_confirmed(self, page: object, default_result: bool) -> bool:
        if self._record_arms:
            self.events.append("confirm")
        return default_result if self._confirm is None else self._confirm

    async def auto_solve_captchas(self, page: object) -> bool:
        if self._record_arms:
            self.events.append("solve")
        return self._auto_solve

    async def solve_recaptcha_token(self, page: object, **kwargs: object) -> bool:
        if self._record_arms:
            self.events.append("token")
        return False


class OcrRecordingAgentFunction(ScopeRecordingAgentFunction):
    def __init__(self, text: str | None, *, enabled: bool = True) -> None:
        super().__init__(record_arms=False)
        self.text = text
        self.enabled = enabled
        self.images: list[bytes] = []

    def supports_image_captcha_ocr(self) -> bool:
        return True

    async def image_captcha_ocr_enabled(self, organization_id: str | None = None, url: str | None = None) -> bool:
        return self.enabled

    async def read_image_captcha_text(
        self, image_png: bytes, *, organization_id: str | None = None, url: str | None = None
    ) -> str | None:
        self.images.append(image_png)
        return self.text


_T = TypeVar("_T")


def stalling_async_mock(entered: asyncio.Event) -> AsyncMock:
    """An awaitable that marks ``entered`` and then never returns, so only cancellation can release it."""

    async def _stall(*args: object, **kwargs: object) -> None:
        entered.set()
        await asyncio.Event().wait()

    return AsyncMock(side_effect=_stall)


async def settle_or_fail(coro: Awaitable[_T], wait_seconds: float = 2.0) -> tuple[asyncio.Task[_T], float]:
    """Run ``coro`` as a task and fail the test if it is still pending after ``wait_seconds``, so a hang fails
    instead of passing as a slow TimeoutError. Returns the finished task and the elapsed seconds."""
    task = asyncio.ensure_future(coro)
    loop = asyncio.get_running_loop()
    started = loop.time()
    await asyncio.wait({task}, timeout=wait_seconds)
    elapsed = loop.time() - started
    if not task.done():
        task.cancel()
        # A task that swallows its cancellation would hang an unbounded await here as well.
        await asyncio.wait({task}, timeout=wait_seconds)
        if task.done():
            with contextlib.suppress(asyncio.CancelledError):
                await task
        pytest.fail(f"task still running after {wait_seconds}s")
    return task, elapsed


class DriverReplyProbe:
    """A driver call that keeps its protocol reply the way playwright and patchright do.

    The pending reply is abandoned from a done-callback on the task that issued the call, and only if
    that task ends cancelled (``_impl/_connection.py::ProtocolCallback``). A caller that is cancelled
    without its task ending therefore leaves the reply live, and the driver's late error lands on it
    with nobody left to retrieve it."""

    def __init__(self, error: BaseException) -> None:
        self._error = error
        self.called = asyncio.Event()
        self.reply: asyncio.Future[Any] | None = None

    async def __call__(self, *args: object, **kwargs: object) -> Any:
        loop = asyncio.get_running_loop()
        reply: asyncio.Future[Any] = loop.create_future()
        self.reply = reply
        self.called.set()
        issuing_task = asyncio.current_task()
        assert issuing_task is not None

        def abandon_reply(task: asyncio.Task[Any]) -> None:
            if task.cancelled():
                reply.cancel()

        issuing_task.add_done_callback(abandon_reply)
        # The driver abandons an unanswered reply on the line after this await, which cancellation skips.
        await asyncio.wait({reply}, return_when=asyncio.FIRST_COMPLETED)
        return reply.result()

    def orphaned_reply(self) -> bool:
        """Deliver the driver's late error now that the caller has given up, and report whether it landed on a reply
        nobody will retrieve (asyncio's never-retrieved line)."""
        assert self.reply is not None, "the driver was never called"
        if not self.reply.done():
            self.reply.set_exception(self._error)
        return not self.reply.cancelled() and self.reply.exception() is not None


def stalled_scrolling_capture(entered: asyncio.Event, timeout_ms: float) -> AsyncMock:
    """A ``take_fullpage_screenshot`` stand-in that drives the real ``take_scrolling_screenshot`` with a stalled
    stitched-capture helper, so a caller test exercises the primitive's own deadline."""
    fake_frame = SimpleNamespace(
        get_scroll_x_y=AsyncMock(return_value=(0, 0)),
        safe_scroll_to_x_y=AsyncMock(return_value=None),
    )

    async def _capture() -> bytes:
        with (
            patch.object(page_module.SkyvernFrame, "create_instance", AsyncMock(return_value=fake_frame)),
            patch.object(page_module, "_scrolling_screenshots_helper", stalling_async_mock(entered)),
        ):
            return await page_module.SkyvernFrame.take_scrolling_screenshot(
                page=MagicMock(name="page"),
                mode=ScreenshotMode.LITE,
                scrolling_number=1,
                timeout=timeout_ms,
            )

    return AsyncMock(side_effect=_capture)


RUN_GROUP_ORG = "o_test"
RUN_GROUP_OTHER_ORG = "o_other"
RUN_GROUP_WPID = "wpid_test"


@dataclass
class FakeExecutor(AsyncExecutor):
    database: AgentDB
    executed: list[str] = field(default_factory=list)
    submitted: list[str] = field(default_factory=list)
    before_queue: Callable[[str], Awaitable[None]] | None = None

    async def execute_task(self, *_: object, **__: object) -> None:
        raise NotImplementedError

    async def execute_task_v2(self, *_: object, **__: object) -> None:
        raise NotImplementedError

    async def execute_script(self, *_: object, **__: object) -> None:
        raise NotImplementedError

    async def execute_workflow(self, *, workflow_run_id: str, **_: object) -> None:
        self.executed.append(workflow_run_id)
        if self.before_queue is not None:
            await self.before_queue(workflow_run_id)
        if await self.database.workflow_runs.update_workflow_run_if_not_final(
            workflow_run_id, WorkflowRunStatus.queued
        ):
            self.submitted.append(workflow_run_id)


@dataclass
class RecordingRateLimiter:
    calls: list[str] = field(default_factory=list)

    async def rate_limit_submit_run(self, organization_id: str) -> None:
        self.calls.append(organization_id)


@dataclass
class GroupEnv:
    database: AgentDB
    executor: FakeExecutor
    organization: Organization
    spawned: list[Coroutine[Any, Any, None]]
    limiter: RecordingRateLimiter


def run_group_definition(*blocks: BlockTypeVar) -> dict[str, Any]:
    return WorkflowDefinition(parameters=[], blocks=list(blocks)).model_dump(mode="json")


def run_group_task_block() -> TaskBlock:
    return TaskBlock(label="login", url="https://example.com", output_parameter=make_block_output_parameter("login"))


async def count_rows(env: GroupEnv, model: type[Base]) -> int:
    async with env.database.Session() as session:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)


@pytest_asyncio.fixture
async def run_group_env(monkeypatch: pytest.MonkeyPatch, sqlite_engine: AsyncEngine) -> AsyncIterator[GroupEnv]:
    database = AgentDB("sqlite+aiosqlite://", db_engine=sqlite_engine)
    organization = await database.organizations.create_organization("Test", organization_id=RUN_GROUP_ORG)
    await database.organizations.create_organization("Other", organization_id=RUN_GROUP_OTHER_ORG)
    async with database.Session() as session:
        session.add(
            WorkflowModel(
                workflow_id="wf_1",
                workflow_permanent_id=RUN_GROUP_WPID,
                organization_id=RUN_GROUP_ORG,
                title="Workflow",
                version=1,
                workflow_definition=run_group_definition(run_group_task_block()),
            )
        )
        session.add_all(
            CredentialModel(
                credential_id=credential_id,
                organization_id=org_id,
                name="Login",
                credential_type="password",
                item_id=f"item_{credential_id}",
            )
            for credential_id, org_id in (
                ("cred_1", RUN_GROUP_ORG),
                ("cred_2", RUN_GROUP_ORG),
                ("cred_foreign", RUN_GROUP_OTHER_ORG),
            )
        )
        await session.commit()
    await database.workflow_params.create_workflow_parameter(
        workflow_id="wf_1", workflow_parameter_type=WorkflowParameterType.CREDENTIAL_ID, key="login", default_value=None
    )
    service = WorkflowService()
    executor = FakeExecutor(database)
    spawned: list[Coroutine[Any, Any, None]] = []
    limiter = RecordingRateLimiter()
    monkeypatch.setattr(app, "DATABASE", database)
    monkeypatch.setattr(object.__getattribute__(app, "_inst"), "RATE_LIMITER", limiter, raising=False)
    monkeypatch.setattr(app, "WORKFLOW_SERVICE", service)
    monkeypatch.setattr(service, "_resolve_managed_browser_profile_for_run_request", AsyncMock(return_value=None))
    monkeypatch.setattr(app.EXPERIMENTATION_PROVIDER, "is_feature_enabled_cached", AsyncMock(return_value=False))
    monkeypatch.setattr(app.AGENT_FUNCTION, "is_block_scoped_workflow_run", AsyncMock(return_value=False))
    monkeypatch.setattr(AsyncExecutorFactory, "get_executor", lambda: executor)
    monkeypatch.setattr(group_service, "_spawn", spawned.append)
    monkeypatch.setattr(service, "_schedule_workflow_run_terminal_hooks", lambda **_: None)
    yield GroupEnv(database, executor, organization, spawned, limiter)
    for coroutine in spawned:
        coroutine.close()
    await asyncio.gather(*app.WORKFLOW_SERVICE._background_tasks, return_exceptions=True)


ORG_VAULT_ITEM_ID = "0f4a3a52-6f4e-4c43-9d37-8f6f2a1c9b10"
ORG_VAULT_LOGIN_ID = f"bwitem_{ORG_VAULT_ITEM_ID}"
ORG_VAULT_ACCOUNT_EMAIL = "vault-owner@example.com"
ORG_VAULT_MASTER_PASSWORD = "ORG_VAULT_MASTER_SECRET"
ORG_VAULT_LOGIN_PASSWORD = "ORG_VAULT_LOGIN_SECRET"


def org_vault_overview(**overrides: Any) -> BitwardenItemOverview:
    fields: dict[str, Any] = {
        "item_id": ORG_VAULT_ITEM_ID,
        "title": "Example portal",
        "collection_id": "col_1",
        "credential_type": CredentialType.PASSWORD,
        "url": "https://portal.example.com/login",
        **overrides,
    }
    fields.setdefault("urls", [fields["url"]] if fields["url"] else [])
    return BitwardenItemOverview(**fields)


@dataclass
class OrgVaultFake:
    listing: AsyncMock
    sites: AsyncMock
    secret: AsyncMock
    org_token: AsyncMock
    folders_restricted: AsyncMock
    organization: Organization


@pytest.fixture
def org_vault(monkeypatch: pytest.MonkeyPatch) -> OrgVaultFake:
    """An organization with a connected Bitwarden account, faked only at the BitwardenService boundary and the
    org-token read. It lists one login whose whole-site URI is portal.example.com."""
    now = datetime(2026, 1, 1, tzinfo=UTC)
    token = BitwardenOrganizationAuthToken(
        id="oat_org_vault",
        organization_id="o_test_org",
        token_type=OrganizationAuthTokenType.bitwarden_credential,
        valid=True,
        created_at=now,
        modified_at=now,
        credential=BitwardenCredential(email=ORG_VAULT_ACCOUNT_EMAIL, master_password=ORG_VAULT_MASTER_PASSWORD),
    )
    organization = Organization(
        organization_id="o_test_org",
        organization_name="Test org",
        bw_collection_ids=["col_1"],
        created_at=now,
        modified_at=now,
    )
    fake = OrgVaultFake(
        listing=AsyncMock(return_value=[org_vault_overview()]),
        sites=AsyncMock(return_value=[LoginSite("https://portal.example.com", whole_site=False)]),
        secret=AsyncMock(
            return_value={
                BitwardenConstants.USERNAME: "vault-user@example.com",
                BitwardenConstants.PASSWORD: ORG_VAULT_LOGIN_PASSWORD,
                BitwardenConstants.TOTP: "",
            }
        ),
        org_token=AsyncMock(return_value=token),
        folders_restricted=AsyncMock(return_value=False),
        organization=organization,
    )
    monkeypatch.setattr(
        app.DATABASE,
        "organizations",
        SimpleNamespace(get_valid_org_auth_token=fake.org_token, get_organization=AsyncMock(return_value=organization)),
        raising=False,
    )
    monkeypatch.setattr(app.AGENT_FUNCTION, "credential_folders_restricted", fake.folders_restricted)
    monkeypatch.setattr(BitwardenService, "list_item_overviews", fake.listing)
    monkeypatch.setattr(BitwardenService, "get_login_item_sites", fake.sites)
    monkeypatch.setattr(BitwardenService, "get_secret_value_from_url", fake.secret)
    return fake
