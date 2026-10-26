"""Fixtures shared by every suite."""

import functools
import gc
import inspect
import io
import logging
import sys
import threading
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
import structlog
import tldextract
import tldextract.tldextract
from _pytest.monkeypatch import NOTSET, derive_importpath
from structlog._config import BoundLoggerLazyProxy

from skyvern.cli.core import action_log
from skyvern.config import settings
from skyvern.forge import app, forge_app_initializer, request_logging
from skyvern.forge.sdk import credential_site_policy
from skyvern.forge.sdk.core import organization_age_cache
from skyvern.forge.sdk.experimentation.code_block_ai_fallback import CODE_BLOCK_AI_FALLBACK_FLAG
from skyvern.forge.sdk.experimentation.providers import NoOpExperimentationProvider
from skyvern.forge.sdk.forge_log import setup_logger
from skyvern.forge.sdk.workflow.models.block import CodeBlock
from skyvern.forge.taskv3 import engine as taskv3_engine
from skyvern.services import organization_log_scope

_monkeypatch_setattr = pytest.MonkeyPatch.setattr
_monkeypatch_undo = pytest.MonkeyPatch.undo


def _setattr_tracking_resolved_logger_methods(
    self: pytest.MonkeyPatch,
    target: str | object,
    name: object | str,
    value: object = NOTSET,
    raising: bool = True,
) -> None:
    if isinstance(target, str) and value is NOTSET:
        attr, obj = derive_importpath(target, raising)
    else:
        attr, obj = name, target
    # A lazy proxy resolves log methods on every access, so a name missing from its instance dict means undo would
    # pin the method it resolved now to the processors of this moment.
    if isinstance(obj, BoundLoggerLazyProxy) and isinstance(attr, str) and attr not in vars(obj):
        self.__dict__.setdefault("_resolved_logger_methods", []).append((obj, attr))
    _monkeypatch_setattr(self, target, name, value, raising)


def _undo_without_stranding_logger_methods(self: pytest.MonkeyPatch) -> None:
    resolved: list[tuple[BoundLoggerLazyProxy, str]] = self.__dict__.pop("_resolved_logger_methods", [])
    _monkeypatch_undo(self)
    for proxy, attr in resolved:
        vars(proxy).pop(attr, None)


pytest.MonkeyPatch.setattr = _setattr_tracking_resolved_logger_methods  # type: ignore[method-assign,assignment]
pytest.MonkeyPatch.undo = _undo_without_stranding_logger_methods  # type: ignore[method-assign]


def pytest_collection_finish(session: pytest.Session) -> None:
    # Every full GC would re-scan the objects imports and collection leave, a main-thread pause long enough to fail
    # short-deadline tests late in a CI shard. Freezing them hides them from the GC and from gc.get_objects().
    gc.collect()
    gc.freeze()


@pytest.fixture(autouse=True, scope="session")
def _configure_logging_once_per_process() -> None:
    """Configure logging the way the app does, once, before the first test runs.

    Left unconfigured, structlog's development renderer draws every logged exception as a rich
    traceback, which takes seconds per call, and it cannot render some values the app logs. Until
    now a test got that only when no earlier file in its process had called setup_logger(), so its
    time and even its outcome depended on how files were split across CI shards: the webhook
    activity tests took 180s alone and 10s after logging was set up, the background task executor
    tests 318s and 42s.
    """
    # setup_logger() also replaces the interpreter's exception hooks; pytest installs its own per test.
    hooks = (sys.excepthook, threading.excepthook, sys.unraisablehook)
    setup_logger()
    sys.excepthook, threading.excepthook, sys.unraisablehook = hooks


def _offline_twin(extractor: tldextract.TLDExtract) -> tldextract.TLDExtract:
    return tldextract.TLDExtract(
        include_psl_private_domains=extractor.include_psl_private_domains, suffix_list_urls=(), cache_dir=None
    )


@pytest.fixture(autouse=True, scope="session")
def _bundled_public_suffix_list() -> Iterator[None]:
    """An extractor downloads the public suffix list on its first call, with no timeout; tests read tldextract's
    bundled snapshot instead, without the disk cache."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(credential_site_policy, "_SITE_EXTRACT", _offline_twin(credential_site_policy._SITE_EXTRACT))
        # tldextract.extract() resolves through this module-level default.
        mp.setattr(tldextract.tldextract, "TLD_EXTRACTOR", _offline_twin(tldextract.tldextract.TLD_EXTRACTOR))
        yield


@pytest.fixture(autouse=True)
def _url_validation_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """URL validation is off by default; tests assert the validated behavior unless they opt out."""
    monkeypatch.setattr(settings, "ENABLE_URL_VALIDATION", True)


@pytest.fixture(autouse=True)
def _no_action_log_delivery(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shared MCP action-log worker posts each cloud-session tool call to SKYVERN_BASE_URL, the public API by
    default. Tests that exercise delivery build their own ActionLogWorker."""
    monkeypatch.setattr(action_log.action_log_worker, "_send_batch", AsyncMock())


LOOP_OWNER = ("skyvern/forge/agent.py", "_execute_task_v3")
# The loop's own contract tests call it directly by design.
LOOP_OWNER_EXEMPT_TEST_FILES = frozenset({"test_taskv3_engine.py"})


def loop_entered_through_executor() -> bool:
    frame = sys._getframe(1)
    while frame is not None:
        code = frame.f_code
        if code.co_name == LOOP_OWNER[1] and code.co_filename.endswith(LOOP_OWNER[0]):
            return True
        frame = frame.f_back
    return False


@pytest.fixture(autouse=True)
def _loop_runs_only_inside_the_executor(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """The runtime half of tests/cloud/test_taskv3_single_loop_call_site.py: it sees calls the static scan cannot,
    such as a name computed at runtime."""
    if request.node.path.name in LOOP_OWNER_EXEMPT_TEST_FILES:
        return
    loop = taskv3_engine.run_task_v3_agent_loop

    @functools.wraps(loop)
    async def guarded(*args: Any, **kwargs: Any) -> taskv3_engine.LoopOutcome:
        if not loop_entered_through_executor():
            raise AssertionError(
                "run_task_v3_agent_loop ran outside ForgeAgent._execute_task_v3; run the task through the executor "
                "(tests/browser_e2e/taskv3_fixture_helpers.run_production_task_v3)"
            )
        return await loop(*args, **kwargs)

    monkeypatch.setattr(taskv3_engine, "run_task_v3_agent_loop", guarded)


@pytest.fixture(autouse=True)
def _isolate_organization_age_cache() -> Iterator[None]:
    """Loading an organization caches its creation time process-wide; no test may leak it into another's logs."""
    yield
    organization_age_cache._created_at_by_organization.clear()
    organization_log_scope._missing_organization_until.clear()
    organization_log_scope._failed_read_until.clear()
    organization_log_scope._warmups_in_flight.clear()


@pytest.fixture
def rendered_log_stream(monkeypatch: pytest.MonkeyPatch) -> Iterator[io.StringIO]:
    """Every log line as production renders it (JSON, raw request logging on), written to a stream."""
    monkeypatch.setattr(request_logging.settings, "LOG_RAW_API_REQUESTS", True)
    monkeypatch.setattr(request_logging.settings, "JSON_LOGGING", True)
    root_logger = logging.getLogger()
    saved_handlers = root_logger.handlers[:]
    saved_structlog_config = structlog.get_config()
    setup_logger()
    # create_api_app configures logging once per process; keep it from replacing this handler.
    monkeypatch.setattr(forge_app_initializer, "_SERVER_LOGGING_CONFIGURED", True)
    stream = io.StringIO()
    handler = root_logger.handlers[0]
    assert isinstance(handler, logging.StreamHandler)
    handler.setStream(stream)
    try:
        yield stream
    finally:
        root_logger.handlers[:] = saved_handlers
        structlog.configure(**saved_structlog_config)


class ForcedSinkFailure(RuntimeError):
    """Stands in for a real sink error when a test forces a side effect to fail."""


@pytest.fixture
def failing_sink(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """Replace a recording sink with one that raises, to assert the caller's decision survives.

    A contained side effect and an uncontained one have byte-identical happy paths, which
    is why review and CI both miss escapes. Making the sink raise is what tells them
    apart::

        failing_sink(workflow_module.workflow, "upsert_search_attributes")

    Pass ``when`` to break only some calls, for the common shape where one sink serves
    both the happy path and a finalizer and only the finalizer's call runs under the
    conditions that break it. It receives the call's arguments; calls it rejects no-op.

    The replacement matches the original's sync/async kind, and patching a name that does
    not exist is an error rather than a silently passing test.
    """

    def install(
        target: object,
        attribute: str,
        *,
        exc: BaseException | None = None,
        when: Callable[..., bool] | None = None,
    ) -> None:
        is_async = inspect.iscoroutinefunction(getattr(target, attribute))
        failure = exc if exc is not None else ForcedSinkFailure(f"{attribute} was forced to fail")

        def should_fail(args: tuple[Any, ...], kwargs: dict[str, Any]) -> bool:
            return when is None or when(*args, **kwargs)

        if is_async:

            async def async_sink(*args: Any, **kwargs: Any) -> None:
                if should_fail(args, kwargs):
                    raise failure

            monkeypatch.setattr(target, attribute, async_sink)
            return

        def sync_sink(*args: Any, **kwargs: Any) -> None:
            if should_fail(args, kwargs):
                raise failure

        monkeypatch.setattr(target, attribute, sync_sink)

    return install


@pytest.fixture
def no_saved_workflow(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give standalone YAML conversion tests an explicit empty saved-workflow lookup."""
    monkeypatch.setattr(app.WORKFLOW_SERVICE, "get_workflow_by_permanent_id", AsyncMock(return_value=None))


class _AiFallbackFlagProvider(NoOpExperimentationProvider):
    enabled_for_org: str | None = None

    async def _is_feature_enabled(self, feature_name: str, distinct_id: str, properties: dict | None = None) -> bool:
        # Org targeting in PostHog reads the person property, so a caller that omits it must resolve False.
        if feature_name != CODE_BLOCK_AI_FALLBACK_FLAG or (properties or {}).get("organization_id") != distinct_id:
            return False
        return distinct_id == self.enabled_for_org


@pytest.fixture
def ai_fallback_flag(monkeypatch: pytest.MonkeyPatch) -> Callable[[str | None], None]:
    """Turn the org-scoped code block AI fallback flag on for one organization id (None: off everywhere), in a
    run that is neither a Copilot build test nor an editor block run."""
    provider = _AiFallbackFlagProvider()
    monkeypatch.setattr(app, "EXPERIMENTATION_PROVIDER", provider)
    monkeypatch.setattr(
        app.DATABASE.workflow_runs,
        "get_workflow_run",
        AsyncMock(
            return_value=SimpleNamespace(
                ai_fallback=None,
                copilot_session_id=None,
                is_debug_session=False,
                parent_workflow_run_id=None,
            )
        ),
    )

    def set_enabled_for_org(organization_id: str | None) -> None:
        provider.enabled_for_org = organization_id
        provider.result_map.clear()

    return set_enabled_for_org


@pytest.fixture
def ai_fallback_on_in_an_ordinary_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the code block AI fallback on, in a run that is neither a Copilot build test nor an editor block run."""
    monkeypatch.setattr(CodeBlock, "_ai_fallback_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(CodeBlock, "_is_authoring_run", AsyncMock(return_value=False))


@pytest.fixture
def copilot_workflow_toggle_off() -> SimpleNamespace:
    return SimpleNamespace(
        ai_fallback=True,
        enable_self_healing=False,
        created_by="copilot",
        edited_by=None,
        workflow_permanent_id="wpid_test",
        organization_id="o_test",
        workflow_definition=None,
    )
