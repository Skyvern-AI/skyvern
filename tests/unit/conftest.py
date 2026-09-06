"""Shared pytest fixtures and setup for unit tests."""

# -- begin speed up unit tests
import itertools
import logging
import shutil
import sys
import threading
from collections.abc import AsyncGenerator, Callable, Iterator
from dataclasses import dataclass
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
from skyvern.forge.sdk.api import files
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.db.models import Base
from skyvern.forge.sdk.workflow.context_manager import WorkflowContextManager
from tests.unit._fingerprint_expectations import FINGERPRINT_TEST_SECRET_KEY
from tests.unit.force_stub_app import start_forge_stub_app

# Four distinct ways to leave the legacy downloads root; each defeats a different weak check.
LEGACY_DOWNLOAD_ESCAPE_CASES = ("parent_traversal", "encoded_dot_dot", "sibling_prefix", "symlink_escape")


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
    ) -> WorkflowContextManager:
        manager = WorkflowContextManager()
        manager.workflow_run_contexts[workflow_run_id] = SimpleNamespace(
            mask_secrets=mask_secrets,
            secrets=dict(secrets or {}),
            runtime_otp_values=set(runtime_otp_values or set()),
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
def restore_interpreter_traceback_hooks() -> Iterator[None]:
    """setup_logger() replaces the three interpreter hooks process-wide.

    Left installed they outlive the test that configured logging and shadow pytest's own
    unraisable/thread-exception plugins, which install their hooks per test.
    """
    hooks = (sys.excepthook, threading.excepthook, sys.unraisablehook)
    yield
    sys.excepthook, threading.excepthook, sys.unraisablehook = hooks


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
