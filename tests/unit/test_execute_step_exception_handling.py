"""Characterization tests for ``ForgeAgent.execute_step`` exception handling (SKY-11786).

These pin the CURRENT observable behavior of the exception handlers at the tail of
``skyvern/forge/agent.py::ForgeAgent.execute_step``. For each exception type they pin:

* whether ``fail_task`` runs,
* whether ``clean_up_task`` runs (and, for the conditional handlers, whether that is
  gated on ``fail_task`` reporting the task as failed),
* the webhook decision (``need_call_webhook``) and the final-screenshot decision
  (``need_final_screenshot``) handed to ``clean_up_task``.

They gate the SKY-11743 restructure (SKY-11787), which collapses the nine cleanup
handlers behind shared per-exception configuration; the suite must pass against current
main with no change to ``execute_step``.

Each handler is reached by making the first in-``try`` await
(``app.AGENT_FUNCTION.validate_step_execution``) raise the target exception, which lands
control directly in the matching ``except`` clause. ``fail_task`` and ``clean_up_task``
are mocked, so the assertions read the decisions off their call args and never touch a
real browser or database. Webhook/screenshot are asserted as *effective* values
(kwarg-or-default), so a restructure that makes the current defaults explicit still
passes — only a change in behavior fails.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, Callable
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from skyvern.config import settings as agent_settings
from skyvern.exceptions import (
    BrowserSessionAlreadyOccupiedError,
    BrowserSessionClosed,
    BrowserSessionOwnershipConflict,
    FailedToNavigateToUrl,
    FailedToParseActionInstruction,
    FailedToSendWebhook,
    InvalidTaskStatusTransition,
    MissingBrowserStatePage,
    ScrapingFailed,
    StepTerminationError,
    StepUnableToExecuteError,
    TaskAlreadyCanceled,
    TaskAlreadyTimeout,
    UnknownErrorWhileCreatingBrowserContext,
    UnsupportedActionType,
    UnsupportedTaskType,
)
from skyvern.forge.agent import ForgeAgent
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.models import Step, StepStatus
from skyvern.forge.sdk.schemas.tasks import Task, TaskStatus
from skyvern.webeye.browser_errors import BrowserTargetClosedError
from tests.unit.helpers import make_organization, make_step, make_task

# ``clean_up_task``'s real signature defaults; handlers that omit these kwargs get them.
_CLEANUP_WEBHOOK_DEFAULT = True
_CLEANUP_SCREENSHOT_DEFAULT = True


@dataclass
class ExecuteStepOutcome:
    returned: tuple | None
    raised: BaseException | None
    fail_task: AsyncMock
    clean_up_task: AsyncMock

    @property
    def fail_task_called(self) -> bool:
        return self.fail_task.await_count > 0

    @property
    def cleanup_called(self) -> bool:
        return self.clean_up_task.await_count > 0

    def _cleanup_kwarg(self, name: str, default: Any) -> Any:
        assert self.cleanup_called, "clean_up_task was not called"
        return self.clean_up_task.await_args.kwargs.get(name, default)

    @property
    def effective_webhook(self) -> bool:
        return bool(self._cleanup_kwarg("need_call_webhook", _CLEANUP_WEBHOOK_DEFAULT))

    @property
    def effective_final_screenshot(self) -> bool:
        return bool(self._cleanup_kwarg("need_final_screenshot", _CLEANUP_SCREENSHOT_DEFAULT))


async def _drive_execute_step(
    monkeypatch: pytest.MonkeyPatch,
    exc: BaseException,
    *,
    fail_task_result: bool = True,
) -> ExecuteStepOutcome:
    """Run ``execute_step`` so that ``exc`` is raised at the first in-``try`` await."""
    agent = ForgeAgent()
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(now, organization)
    step = make_step(now, task, step_id="step-0", status=StepStatus.running, order=0, output=None)

    fail_task_mock = AsyncMock(return_value=fail_task_result)
    clean_up_task_mock = AsyncMock(return_value=None)
    agent.fail_task = fail_task_mock  # type: ignore[method-assign]
    agent.clean_up_task = clean_up_task_mock  # type: ignore[method-assign]

    # Pre-``try`` DB reads in execute_step: keep them inert so we reach the try body.
    monkeypatch.setattr("skyvern.forge.agent.app.DATABASE.tasks.get_task", AsyncMock(return_value=None))
    monkeypatch.setattr("skyvern.forge.agent.app.DATABASE.tasks.update_task", AsyncMock(return_value=task))
    # First in-``try`` await; raising here routes straight to the matching except clause.
    monkeypatch.setattr(
        "skyvern.forge.agent.app.AGENT_FUNCTION.validate_step_execution",
        AsyncMock(side_effect=exc),
    )

    context = SkyvernContext(
        organization_id=organization.organization_id,
        task_id=task.task_id,
        step_id=None,
        tz_info=ZoneInfo("UTC"),
    )
    skyvern_context.set(context)
    returned: tuple | None = None
    raised: BaseException | None = None
    try:
        returned = await agent.execute_step(
            organization=organization,
            task=task,
            step=step,
            api_key="api-key",
            download_baseline_files=[],
        )
    except BaseException as caught:  # noqa: BLE001 - we characterize which exceptions propagate
        raised = caught
    finally:
        skyvern_context.reset()

    return ExecuteStepOutcome(
        returned=returned,
        raised=raised,
        fail_task=fail_task_mock,
        clean_up_task=clean_up_task_mock,
    )


@dataclass(frozen=True)
class CleanupHandlerCase:
    """Pinned contract for one cleanup ``except`` clause in ``execute_step``."""

    id: str
    exc_factory: Callable[[], BaseException]
    fail_task_called: bool
    effective_webhook: bool
    effective_final_screenshot: bool
    # True => clean_up_task runs only when fail_task reports the task as failed.
    cleanup_gated_on_fail_task: bool


# The nine cleanup ``except`` clauses (SKY-11743's "nine cleanup handlers"). The
# unsupported-* clause catches three exception types and is exercised by all three below.
CLEANUP_CASES: list[CleanupHandlerCase] = [
    CleanupHandlerCase(
        id="task_already_timeout",
        exc_factory=lambda: TaskAlreadyTimeout("task-123"),
        fail_task_called=False,
        effective_webhook=True,
        effective_final_screenshot=True,
        cleanup_gated_on_fail_task=False,
    ),
    CleanupHandlerCase(
        id="step_termination",
        exc_factory=lambda: StepTerminationError("terminated", step_id="step-0", task_id="task-123"),
        fail_task_called=True,
        effective_webhook=True,
        effective_final_screenshot=True,
        cleanup_gated_on_fail_task=True,
    ),
    CleanupHandlerCase(
        id="failed_to_navigate",
        exc_factory=lambda: FailedToNavigateToUrl("https://example.com", "boom"),
        fail_task_called=True,
        effective_webhook=True,
        effective_final_screenshot=False,  # the only handler that suppresses the final screenshot
        cleanup_gated_on_fail_task=True,
    ),
    CleanupHandlerCase(
        id="task_already_canceled",
        exc_factory=lambda: TaskAlreadyCanceled("failed", "task-123"),
        fail_task_called=False,
        effective_webhook=False,
        effective_final_screenshot=True,
        cleanup_gated_on_fail_task=False,
    ),
    CleanupHandlerCase(
        id="invalid_status_transition",
        exc_factory=lambda: InvalidTaskStatusTransition("running", "failed", "task-123"),
        fail_task_called=False,
        effective_webhook=False,
        effective_final_screenshot=True,
        cleanup_gated_on_fail_task=False,
    ),
    CleanupHandlerCase(
        id="unsupported_action_type",
        exc_factory=lambda: UnsupportedActionType("MYSTERY_ACTION"),
        fail_task_called=True,
        effective_webhook=False,
        effective_final_screenshot=True,
        cleanup_gated_on_fail_task=False,
    ),
    CleanupHandlerCase(
        id="unsupported_task_type",
        exc_factory=lambda: UnsupportedTaskType("mystery_task"),
        fail_task_called=True,
        effective_webhook=False,
        effective_final_screenshot=True,
        cleanup_gated_on_fail_task=False,
    ),
    CleanupHandlerCase(
        id="failed_to_parse_action",
        exc_factory=lambda: FailedToParseActionInstruction("bad", "ValueError"),
        fail_task_called=True,
        effective_webhook=False,
        effective_final_screenshot=True,
        cleanup_gated_on_fail_task=False,
    ),
    CleanupHandlerCase(
        id="scraping_failed",
        exc_factory=lambda: ScrapingFailed(reason="page gone"),
        fail_task_called=True,
        effective_webhook=True,
        effective_final_screenshot=True,
        cleanup_gated_on_fail_task=False,
    ),
    CleanupHandlerCase(
        id="missing_browser_state_page",
        exc_factory=lambda: MissingBrowserStatePage(task_id="task-123"),
        fail_task_called=True,
        effective_webhook=True,
        effective_final_screenshot=True,
        cleanup_gated_on_fail_task=False,
    ),
    CleanupHandlerCase(
        id="generic_exception",
        exc_factory=lambda: RuntimeError("something unexpected"),
        fail_task_called=True,
        effective_webhook=True,
        effective_final_screenshot=True,
        cleanup_gated_on_fail_task=True,
    ),
    # SKY-15xxx: these four are Skyvern's own named browser/session exceptions (already
    # anticipated, unlike a true "unexpected" error) that used to fall through to the
    # generic_exception handler above and page P1 on every occurrence. They now share a
    # dedicated LOG.warning handler with the same fail_task/clean_up_task shape as
    # generic_exception, just without the error-level page. fail_task/clean_up_task shape
    # is identical here regardless of whether the wrapped cause is recognized (see the
    # dedicated log-level tests below for that split).
    CleanupHandlerCase(
        id="unknown_error_creating_browser_context",
        exc_factory=lambda: UnknownErrorWhileCreatingBrowserContext("chromium", RuntimeError("boom")),
        fail_task_called=True,
        effective_webhook=True,
        effective_final_screenshot=True,
        cleanup_gated_on_fail_task=True,
    ),
    CleanupHandlerCase(
        id="browser_session_already_occupied",
        exc_factory=lambda: BrowserSessionAlreadyOccupiedError("pbs-1", "tsk-2"),
        fail_task_called=True,
        effective_webhook=True,
        effective_final_screenshot=True,
        cleanup_gated_on_fail_task=True,
    ),
    CleanupHandlerCase(
        id="browser_session_closed",
        exc_factory=lambda: BrowserSessionClosed("pbs-1"),
        fail_task_called=True,
        effective_webhook=True,
        effective_final_screenshot=True,
        cleanup_gated_on_fail_task=True,
    ),
    CleanupHandlerCase(
        id="browser_session_ownership_conflict",
        exc_factory=lambda: BrowserSessionOwnershipConflict("pbs-1"),
        fail_task_called=True,
        effective_webhook=True,
        effective_final_screenshot=True,
        cleanup_gated_on_fail_task=True,
    ),
    CleanupHandlerCase(
        id="browser_target_closed",
        exc_factory=lambda: BrowserTargetClosedError("session closed underneath the run"),
        fail_task_called=True,
        effective_webhook=True,
        effective_final_screenshot=True,
        cleanup_gated_on_fail_task=True,
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CLEANUP_CASES, ids=[c.id for c in CLEANUP_CASES])
async def test_cleanup_handler_pins_webhook_screenshot_and_fail_task(
    monkeypatch: pytest.MonkeyPatch, case: CleanupHandlerCase
) -> None:
    """Each cleanup handler runs clean_up_task with a pinned webhook/screenshot decision.

    Driven with fail_task reporting the task as failed, so the gated handlers also clean up.
    """
    outcome = await _drive_execute_step(monkeypatch, case.exc_factory(), fail_task_result=True)

    assert outcome.raised is None, f"{case.id} unexpectedly propagated {outcome.raised!r}"
    assert outcome.fail_task_called is case.fail_task_called
    assert outcome.cleanup_called is True
    assert outcome.effective_webhook is case.effective_webhook
    assert outcome.effective_final_screenshot is case.effective_final_screenshot
    # Every cleanup handler returns (step, detailed_output, next_step); nothing advanced here.
    assert outcome.returned is not None
    assert outcome.returned[0].step_id == "step-0"
    assert outcome.returned[2] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CLEANUP_CASES, ids=[c.id for c in CLEANUP_CASES])
async def test_cleanup_gating_when_fail_task_reports_not_failed(
    monkeypatch: pytest.MonkeyPatch, case: CleanupHandlerCase
) -> None:
    """Cleanup runs exactly once on every handler, whatever fail_task reports: the gated handlers
    skip their own ``clean_up_task`` and the outer ``finally`` runs it, while the unconditional
    handlers reach it directly and keep their own webhook/screenshot decisions. The recovery must
    not add a second call on top of either.
    """
    outcome = await _drive_execute_step(monkeypatch, case.exc_factory(), fail_task_result=False)

    assert outcome.raised is None
    assert outcome.clean_up_task.await_count == 1
    if case.cleanup_gated_on_fail_task:
        # The recovery's call, not the handler's. It recovers the download half only: the row was
        # finalized by someone else who webhooks itself, and the browser may not be usable.
        assert outcome.effective_webhook is False
        assert outcome.effective_final_screenshot is False
    else:
        assert outcome.effective_webhook is case.effective_webhook
        assert outcome.effective_final_screenshot is case.effective_final_screenshot


@pytest.mark.asyncio
async def test_unknown_browser_context_error_pages_p1_for_unrecognized_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BrowserFactory.create_browser_context wraps *any* exception, so a cause that isn't
    one of Skyvern's own named exceptions (e.g. a real bug's AttributeError) is not an
    anticipated browser/session failure -- it must keep the error-level page, not go quiet.
    """
    log_mock = MagicMock()
    monkeypatch.setattr("skyvern.forge.agent.LOG", log_mock)

    cause = AttributeError("'NoneType' object has no attribute 'frame'")
    exc = UnknownErrorWhileCreatingBrowserContext("chromium", cause)
    exc.__cause__ = cause  # mirrors `raise ... from e` at the real call site

    outcome = await _drive_execute_step(monkeypatch, exc, fail_task_result=True)

    assert outcome.raised is None
    assert outcome.fail_task_called is True
    assert outcome.cleanup_called is True
    log_mock.exception.assert_called_once_with("Got an unexpected exception in step, marking task as failed")
    log_mock.warning.assert_not_called()


@pytest.mark.asyncio
async def test_unknown_browser_context_error_stays_quiet_for_recognized_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cause that IS one of Skyvern's own named exceptions (e.g. a proxy-capacity error
    surfacing as MissingBrowserStatePage) is the anticipated case this handler exists for --
    it must stay on the quiet, non-paging path.
    """
    log_mock = MagicMock()
    monkeypatch.setattr("skyvern.forge.agent.LOG", log_mock)

    cause = MissingBrowserStatePage(task_id="task-123")
    exc = UnknownErrorWhileCreatingBrowserContext("chromium", cause)
    exc.__cause__ = cause

    outcome = await _drive_execute_step(monkeypatch, exc, fail_task_result=True)

    assert outcome.raised is None
    assert outcome.fail_task_called is True
    assert outcome.cleanup_called is True
    log_mock.warning.assert_called_once()
    log_mock.exception.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc_factory",
    [
        lambda: BrowserSessionAlreadyOccupiedError("pbs-1", "tsk-2"),
        lambda: BrowserSessionClosed("pbs-1"),
        lambda: BrowserSessionOwnershipConflict("pbs-1"),
        lambda: BrowserTargetClosedError("session closed underneath the run"),
    ],
    ids=[
        "browser_session_already_occupied",
        "browser_session_closed",
        "browser_session_ownership_conflict",
        "browser_target_closed",
    ],
)
async def test_direct_session_exceptions_stay_on_the_quiet_path(
    monkeypatch: pytest.MonkeyPatch, exc_factory: Callable[[], BaseException]
) -> None:
    """These four are Skyvern's own named browser/session exceptions -- unlike
    UnknownErrorWhileCreatingBrowserContext's conditional handling, they're unconditionally
    quiet. CLEANUP_CASES pins fail_task/clean_up_task/webhook/screenshot, which stay
    identical to generic_exception's -- only the log level tells these apart, so pin that
    directly: dropping any one of the four from the tuple must fail this test.
    """
    log_mock = MagicMock()
    monkeypatch.setattr("skyvern.forge.agent.LOG", log_mock)

    outcome = await _drive_execute_step(monkeypatch, exc_factory(), fail_task_result=True)

    assert outcome.raised is None
    log_mock.warning.assert_called_once()
    log_mock.exception.assert_not_called()


@pytest.mark.asyncio
async def test_step_unable_to_execute_reraises_without_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    """StepUnableToExecuteError propagates out of execute_step; no fail_task, no cleanup."""
    outcome = await _drive_execute_step(monkeypatch, StepUnableToExecuteError("step-0", "cannot run"))

    assert isinstance(outcome.raised, StepUnableToExecuteError)
    assert outcome.returned is None
    assert outcome.fail_task_called is False
    assert outcome.cleanup_called is False


@pytest.mark.asyncio
async def test_failed_to_send_webhook_is_swallowed_without_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    """FailedToSendWebhook is swallowed (step returned); it never fails or cleans up the task."""
    outcome = await _drive_execute_step(monkeypatch, FailedToSendWebhook(task_id="task-123"))

    assert outcome.raised is None
    assert outcome.fail_task_called is False
    assert outcome.cleanup_called is False
    assert outcome.returned is not None
    assert outcome.returned[0].step_id == "step-0"
    assert outcome.returned[2] is None


# SKY-13472: execute_step recurses into the next step via `return await self.execute_step(...)`.
# The parent frame's ``detailed_output`` (holding that step's ``scraped_page``) would otherwise stay
# alive on the stack for the whole recursive subtree, pinning one ``ScrapedPage`` per level. Once
# ``record_fail_fast_shadow`` and ``handle_completed_step`` have consumed it, nothing else reads it
# before the child runs, so it is released. These drive one real ``execute_step`` frame to each of
# the two recursive call sites and, via a spy on the recursive entry, assert the parent's
# ``scraped_page`` is already cleared when the child begins — while proving the pre-release consumers
# still saw the real page and the child's return value is forwarded unchanged.
@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["retry", "execute_all_steps"], ids=["retry-site", "execute-all-steps-site"])
async def test_execute_step_releases_scraped_page_before_recursion(monkeypatch: pytest.MonkeyPatch, path: str) -> None:
    agent = ForgeAgent()
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(now, organization)
    step = make_step(now, task, step_id="step-0", status=StepStatus.running, order=0, output=None)
    next_step = make_step(now, task, step_id="step-1", status=StepStatus.running, order=1, output=None)

    scraped_page = object()
    detailed_output = SimpleNamespace(scraped_page=scraped_page, cua_response=None)

    step_after = make_step(
        now,
        task,
        step_id="step-0",
        status=StepStatus.failed if path == "retry" else StepStatus.completed,
        order=0,
        output=None,
    )

    browser_state = MagicMock()
    browser_state.get_working_page = AsyncMock(return_value=MagicMock())

    monkeypatch.setattr("skyvern.forge.agent.app.DATABASE.tasks.get_task", AsyncMock(return_value=None))
    monkeypatch.setattr("skyvern.forge.agent.app.DATABASE.tasks.update_task", AsyncMock(return_value=task))
    monkeypatch.setattr("skyvern.forge.agent.app.AGENT_FUNCTION.validate_step_execution", AsyncMock(return_value=None))
    monkeypatch.setattr("skyvern.forge.agent.app.AGENT_FUNCTION.post_step_execution", AsyncMock(return_value=None))
    monkeypatch.setattr("skyvern.forge.agent.app.ARTIFACT_MANAGER.flush_step_archive", AsyncMock(return_value=None))
    monkeypatch.setattr(type(agent_settings), "execute_all_steps", lambda self: True)

    fail_fast_spy = AsyncMock(return_value=None)
    monkeypatch.setattr("skyvern.forge.agent.record_fail_fast_shadow", fail_fast_spy)

    agent.initialize_execution_state = AsyncMock(return_value=(step, browser_state, detailed_output))  # type: ignore[method-assign]
    agent.register_async_operations = AsyncMock(return_value=None)  # type: ignore[method-assign]
    agent.agent_step = AsyncMock(return_value=(step_after, detailed_output))  # type: ignore[method-assign]
    agent.update_task_errors_from_detailed_output = AsyncMock(return_value=task)  # type: ignore[method-assign]
    agent._sync_video_artifact_after_step = AsyncMock(return_value=None)  # type: ignore[method-assign]
    handle_completed_step = AsyncMock(return_value=(None, None, next_step))
    handle_failed_step = AsyncMock(return_value=next_step)
    agent.handle_completed_step = handle_completed_step  # type: ignore[method-assign]
    agent.handle_failed_step = handle_failed_step  # type: ignore[method-assign]

    snapshots: dict[str, Any] = {}
    calls = {"n": 0}
    real_execute_step = agent.execute_step

    async def spy(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            return await real_execute_step(*args, **kwargs)
        snapshots["scraped_page_at_child_entry"] = detailed_output.scraped_page
        child_step = kwargs["step"] if "step" in kwargs else args[2]
        return (child_step, None, None)

    agent.execute_step = spy  # type: ignore[method-assign]

    context = SkyvernContext(
        organization_id=organization.organization_id, task_id=task.task_id, tz_info=ZoneInfo("UTC")
    )
    skyvern_context.set(context)
    try:
        result = await agent.execute_step(
            organization=organization,
            task=task,
            step=step,
            api_key="api-key",
            download_baseline_files=[],
        )
    finally:
        skyvern_context.reset()

    # The recursion happened, and the child's return value is forwarded verbatim.
    assert calls["n"] == 2
    assert result == (next_step, None, None)

    # The parent's scraped_page is already released when the child begins.
    assert snapshots["scraped_page_at_child_entry"] is None
    assert detailed_output.scraped_page is None

    # Consumers that run before the release still saw the real page (behavior unchanged).
    assert fail_fast_spy.await_args.kwargs["scraped_page"] is scraped_page
    if path == "execute_all_steps":
        assert handle_completed_step.await_args.kwargs["scraped_page"] is scraped_page


# These drive the REAL ``fail_task`` and ``clean_up_task``, because mocking exactly those two hides
# that most handlers clean up unconditionally. Only the browser, storage and webhook edges are faked,
# and ``fail_task`` genuinely returns False because the row it is handed is already final.


@dataclass
class RealCleanupOutcome:
    raised: BaseException | None
    saves: AsyncMock
    cleanup_entries: list[dict[str, Any]]
    downloads_recorded: bool
    context_cleared: bool


async def _drive_execute_step_real_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    *,
    in_try: Callable[[ForgeAgent, Task, Step, dict[str, bool]], Any],
    task_block: Any = None,
    cleanup_raises: BaseException | None = None,
) -> RealCleanupOutcome:
    """Run ``execute_step`` against an already-final task row with real fail_task/clean_up_task.

    ``in_try`` replaces the first in-``try`` await, standing in for whatever the try body did before
    raising -- including entering ``clean_up_task`` itself, which is the shape ``_execute_task_v3``
    has: its last statement before returning is a ``clean_up_task`` call. Its fourth argument is a
    knob dict; setting ``knobs["db_down"]`` makes the task-refresh read fail.
    """
    agent = ForgeAgent()
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(now, organization)
    step = make_step(now, task, step_id="step-0", status=StepStatus.running, order=0, output=None)
    # The already-final row: Task.validate_update refuses completed -> failed with
    # InvalidTaskStatusTransition, which is what makes the real fail_task return False.
    final_task = make_task(now, organization, status=TaskStatus.completed, extracted_information="done")
    knobs: dict[str, bool] = {"db_down": False}

    async def get_task(*_args: Any, **_kwargs: Any) -> Task:
        if knobs["db_down"]:
            raise RuntimeError("database unavailable")
        return final_task

    monkeypatch.setattr("skyvern.forge.agent.app.DATABASE.tasks.get_task", get_task)
    monkeypatch.setattr("skyvern.forge.agent.app.DATABASE.tasks.get_task_steps", AsyncMock(return_value=[]))
    monkeypatch.setattr("skyvern.forge.agent.app.DATABASE.tasks.update_task", AsyncMock(return_value=final_task))
    monkeypatch.setattr("skyvern.forge.agent.app.DATABASE.tasks.update_step", AsyncMock(return_value=step))
    monkeypatch.setattr("skyvern.forge.agent.save_step_logs", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER.artifact_redaction_enabled", MagicMock(return_value=False)
    )
    monkeypatch.setattr(
        "skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER.runtime_secret_values_for_artifacts",
        MagicMock(return_value=[]),
    )
    # The browser, storage and webhook edges clean_up_task reaches out to, all downstream of the
    # download save this suite asserts on.
    monkeypatch.setattr("skyvern.forge.agent.app.BROWSER_MANAGER.get_for_task", MagicMock(return_value=None))
    monkeypatch.setattr("skyvern.forge.agent.analytics.capture", MagicMock(return_value=None))
    monkeypatch.setattr("skyvern.forge.agent.drain_speculative_persist_tasks", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "skyvern.forge.agent.uploaded_file_service.delete_files_attached_to_run", AsyncMock(return_value=None)
    )
    monkeypatch.setattr("skyvern.forge.agent.app.ARTIFACT_MANAGER.wait_for_upload_aiotasks", AsyncMock())
    agent.async_operation_pool.remove_task = AsyncMock()  # type: ignore[method-assign]
    agent.cleanup_browser_and_create_artifacts = AsyncMock()  # type: ignore[method-assign]
    agent.execute_task_webhook = AsyncMock()  # type: ignore[method-assign]
    # The observation point: the download save clean_up_task performs, which is the work the ticket
    # exists to stop losing.
    saves = AsyncMock(return_value=None)
    monkeypatch.setattr("skyvern.forge.agent.app.STORAGE.save_downloaded_files", saves)

    # A spy, not a mock: it counts entries and calls straight through, so the real clean_up_task --
    # and therefore the real marker -- still runs.
    cleanup_entries: list[dict[str, Any]] = []
    real_cleanup = agent.clean_up_task

    async def counting_cleanup(**kwargs: Any) -> None:
        cleanup_entries.append(kwargs)
        if cleanup_raises is not None:
            raise cleanup_raises
        return await real_cleanup(**kwargs)

    agent.clean_up_task = counting_cleanup  # type: ignore[method-assign]

    async def first_in_try_await(*_args: Any, **_kwargs: Any) -> None:
        await in_try(agent, task, step, knobs)

    monkeypatch.setattr("skyvern.forge.agent.app.AGENT_FUNCTION.validate_step_execution", first_in_try_await)

    context = SkyvernContext(
        organization_id=organization.organization_id,
        task_id=task.task_id,
        step_id=None,
        tz_info=ZoneInfo("UTC"),
    )
    skyvern_context.set(context)
    raised: BaseException | None = None
    try:
        await agent.execute_step(
            organization=organization,
            task=task,
            step=step,
            api_key="api-key",
            task_block=task_block,
            download_baseline_files=[],
        )
    except BaseException as caught:  # noqa: BLE001 - propagation is part of what is characterized
        raised = caught
    finally:
        downloads_recorded = context.cleanup_downloads_recorded(task.task_id)
        context_cleared = (
            context.step_id is None
            and context.task_id is None
            and context.navigation_goal is None
            and context.navigation_payload is None
        )
        skyvern_context.reset()

    return RealCleanupOutcome(
        raised=raised,
        saves=saves,
        cleanup_entries=cleanup_entries,
        downloads_recorded=downloads_recorded,
        context_cleared=context_cleared,
    )


@pytest.mark.asyncio
async def test_handler_that_cannot_fail_the_task_still_records_its_downloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ticket's case: the row is already final, so the real fail_task returns False and the
    gated handler skips cleanup. The download half must still run, exactly once.

    RED against origin/main, where nothing recovers the skipped cleanup and the save never happens.
    """

    async def raise_against_a_final_row(*_args: Any) -> None:
        raise RuntimeError("failed after the task row was finalized")

    outcome = await _drive_execute_step_real_cleanup(monkeypatch, in_try=raise_against_a_final_row)

    assert outcome.raised is None
    assert len(outcome.cleanup_entries) == 1
    assert outcome.saves.await_count == 1
    assert outcome.downloads_recorded is True
    # The recovery takes the download half and declines the rest: the external finalizer that made
    # this row terminal webhooks itself, and finalizing without that caller's baseline would rename
    # files belonging to another block.
    recovery = outcome.cleanup_entries[0]
    assert recovery["need_call_webhook"] is False
    assert recovery["need_final_screenshot"] is False
    assert recovery["download_suffix"] is None
    assert "list_files_before" not in recovery


@pytest.mark.asyncio
async def test_recovery_does_not_rerun_a_cleanup_that_already_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exactly once, not at least once.

    ``_execute_task_v3``'s last statement before returning is a ``clean_up_task`` call, so a raise
    just after it reaches the handlers with the downloads already recorded. A second pass would not
    duplicate the DOWNLOAD row -- ``save_downloaded_files`` dedupes on ``{base_uri}/{file}`` plus
    checksum, and the recovery does not rename -- but it would spend another settle-and-checksum
    budget on a teardown path and record the download outcome a second time.
    """

    async def clean_up_then_raise(agent: ForgeAgent, task: Task, step: Step, _knobs: dict[str, bool]) -> None:
        await agent.clean_up_task(task=task, last_step=step)
        raise RuntimeError("raised after cleanup had already recorded the downloads")

    outcome = await _drive_execute_step_real_cleanup(monkeypatch, in_try=clean_up_then_raise)

    assert outcome.raised is None
    # One entry, from the try body: the recovery saw the downloads recorded and stood down.
    assert len(outcome.cleanup_entries) == 1
    assert outcome.saves.await_count == 1


@pytest.mark.asyncio
async def test_cleanup_that_died_before_the_download_half_does_not_claim_it_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cleanup that dies before the download half must leave the recovery free to run.

    ``clean_up_task``'s task refresh converts any db failure into ``TaskNotFound`` and raises it
    long before the download half starts. Recording the marker on entry would let that raise
    suppress the recovery and lose the download outright, so the marker records the download half
    having run rather than the function having been entered.

    Scope: this drives the cleanup that dies from inside the ``try`` body, which is the shape
    ``_execute_task_v3`` has. A ``clean_up_task`` that dies the same way from inside a *handler*
    propagates out of ``execute_step`` instead of returning, and is not recovered here -- a
    pre-existing gap shared by all sixteen handlers, called out in the PR rather than fixed.
    """

    async def clean_up_with_a_dead_db(agent: ForgeAgent, task: Task, step: Step, knobs: dict[str, bool]) -> None:
        knobs["db_down"] = True
        try:
            await agent.clean_up_task(task=task, last_step=step)
        finally:
            knobs["db_down"] = False

    outcome = await _drive_execute_step_real_cleanup(monkeypatch, in_try=clean_up_with_a_dead_db)

    assert outcome.raised is None
    # Two entries: the one that died at the refresh, and the recovery that actually recorded.
    assert len(outcome.cleanup_entries) == 2
    assert outcome.saves.await_count == 1
    assert outcome.downloads_recorded is True


@pytest.mark.asyncio
async def test_recovery_does_not_rename_a_block_download_to_its_suffix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The recovery must not carry the block's ``download_suffix`` into cleanup's finalize.

    Finalizing renames every file the baseline did not already list. The baseline that makes that
    safe is built by the caller that ran the loop -- ``_execute_task_v3`` augments one with the
    files its own ``file_upload`` tool staged, and never receives this frame's. Finalizing against
    this frame's raw pre-step listing would rename staged inputs, and any earlier block's downloads,
    to this block's suffix. Leaving the download under the site's own name is the lesser outcome.

    Also the only test here that drives a gated handler other than the generic ``except Exception``.
    """
    task_block = MagicMock()
    task_block.download_suffix = "block-suffix"
    task_block.complete_on_download = False

    async def fail_to_navigate(*_args: Any) -> None:
        raise FailedToNavigateToUrl("https://example.com", "boom")

    outcome = await _drive_execute_step_real_cleanup(monkeypatch, in_try=fail_to_navigate, task_block=task_block)

    assert outcome.raised is None
    assert len(outcome.cleanup_entries) == 1
    assert outcome.saves.await_count == 1
    assert outcome.cleanup_entries[0]["download_suffix"] is None


@pytest.mark.asyncio
async def test_cancellation_inside_the_recovery_still_clears_the_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancellation during the recovery must propagate and still leave the context cleared.

    The recovery added an await to a ``finally`` that previously had none, so a ``CancelledError``
    -- a ``BaseException``, which ``contained_effect`` deliberately does not contain -- would
    otherwise exit before the context reset and leave the run describing a task that is over.
    """

    async def raise_against_a_final_row(*_args: Any) -> None:
        raise RuntimeError("failed after the task row was finalized")

    outcome = await _drive_execute_step_real_cleanup(
        monkeypatch,
        in_try=raise_against_a_final_row,
        cleanup_raises=asyncio.CancelledError(),
    )

    assert isinstance(outcome.raised, asyncio.CancelledError)
    assert outcome.context_cleared is True
