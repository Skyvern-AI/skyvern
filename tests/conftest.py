"""Fixtures shared by every suite."""

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

from skyvern.forge import app, forge_app_initializer, request_logging
from skyvern.forge.sdk.core import organization_age_cache
from skyvern.forge.sdk.experimentation.code_block_ai_fallback import CODE_BLOCK_AI_FALLBACK_FLAG
from skyvern.forge.sdk.experimentation.providers import NoOpExperimentationProvider
from skyvern.forge.sdk.forge_log import setup_logger
from skyvern.forge.sdk.workflow.models.block import CodeBlock
from skyvern.services import organization_log_scope


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
            return_value=SimpleNamespace(copilot_session_id=None, is_debug_session=False, parent_workflow_run_id=None)
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
        enable_self_healing=False,
        created_by="copilot",
        edited_by=None,
        workflow_permanent_id="wpid_test",
        organization_id="o_test",
        workflow_definition=None,
    )
